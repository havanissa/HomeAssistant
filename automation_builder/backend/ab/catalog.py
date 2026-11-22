"""What Automation Builder knows about Home Assistant's controllable domains.

* which domains can be a controlled target;
* the ready-made actions offered for each domain (the user can always enter any
  other action by hand);
* the *effect* of an action: the state it leaves an entity in, when that is
  known. The compiler and the conflict check use effects to decide whether an
  action may be repeated safely (occupancy rechecks), whether the intended state
  is already satisfied, and whether two rules contradict each other.

Nothing here is specific to lights: a domain that is not listed can still be
controlled through a hand-entered action, it only gets no ready-made actions.
"""

from __future__ import annotations

from typing import Any

ON_OFF = [
    {"id": "turn_on", "label": "Turn on", "service": "turn_on"},
    {"id": "turn_off", "label": "Turn off", "service": "turn_off"},
    {"id": "toggle", "label": "Toggle", "service": "toggle"},
]


def _field(key: str, label: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "kind": kind, **extra}


LIGHT_ON_FIELDS = [
    _field("brightness_pct", "Brightness", "number", min=1, max=100, unit="%"),
    _field("color_temp_kelvin", "Colour temperature", "number", min=1500, max=9000, unit="K"),
    _field("rgb_color", "Colour", "color"),
    _field("transition", "Transition", "number", min=0, max=300, unit="s"),
]

#: domain -> {"label", "actions": [{id, label, service, fields?}]}
DOMAINS: dict[str, dict[str, Any]] = {
    "light": {"label": "Light", "actions": [
        {"id": "turn_on", "label": "Turn on", "service": "turn_on", "fields": LIGHT_ON_FIELDS},
        {"id": "turn_off", "label": "Turn off", "service": "turn_off",
         "fields": [_field("transition", "Transition", "number", min=0, max=300, unit="s")]},
        {"id": "toggle", "label": "Toggle", "service": "toggle"}]},
    "switch": {"label": "Switch or relay", "actions": ON_OFF},
    "input_boolean": {"label": "Toggle helper", "actions": ON_OFF},
    "fan": {"label": "Fan", "actions": [
        {"id": "turn_on", "label": "Turn on", "service": "turn_on",
         "fields": [_field("percentage", "Speed", "number", min=1, max=100, unit="%")]},
        {"id": "turn_off", "label": "Turn off", "service": "turn_off"},
        {"id": "toggle", "label": "Toggle", "service": "toggle"},
        {"id": "set_percentage", "label": "Set speed", "service": "set_percentage",
         "fields": [_field("percentage", "Speed", "number", min=0, max=100, unit="%", required=True)]}]},
    "cover": {"label": "Cover", "actions": [
        {"id": "open_cover", "label": "Open", "service": "open_cover"},
        {"id": "close_cover", "label": "Close", "service": "close_cover"},
        {"id": "stop_cover", "label": "Stop", "service": "stop_cover"},
        {"id": "set_cover_position", "label": "Set position", "service": "set_cover_position",
         "fields": [_field("position", "Position (100 = open)", "number", min=0, max=100, unit="%", required=True)]},
        {"id": "toggle", "label": "Toggle", "service": "toggle"}]},
    "climate": {"label": "Climate", "actions": [
        {"id": "set_temperature", "label": "Set temperature", "service": "set_temperature",
         "fields": [_field("temperature", "Temperature", "number", min=-50, max=120, step=0.5, unit="°", required=True)]},
        {"id": "set_hvac_mode", "label": "Set mode", "service": "set_hvac_mode",
         "fields": [_field("hvac_mode", "Mode", "option", attribute="hvac_modes", required=True)]},
        {"id": "set_preset_mode", "label": "Set preset", "service": "set_preset_mode",
         "fields": [_field("preset_mode", "Preset", "option", attribute="preset_modes", required=True)]},
        {"id": "turn_on", "label": "Turn on", "service": "turn_on"},
        {"id": "turn_off", "label": "Turn off", "service": "turn_off"}]},
    "humidifier": {"label": "Humidifier", "actions": [
        *ON_OFF,
        {"id": "set_humidity", "label": "Set humidity", "service": "set_humidity",
         "fields": [_field("humidity", "Humidity", "number", min=0, max=100, unit="%", required=True)]}]},
    "lock": {"label": "Lock", "actions": [
        {"id": "lock", "label": "Lock", "service": "lock"}, {"id": "unlock", "label": "Unlock", "service": "unlock"}]},
    "valve": {"label": "Valve", "actions": [
        {"id": "open_valve", "label": "Open", "service": "open_valve"},
        {"id": "close_valve", "label": "Close", "service": "close_valve"}]},
    "siren": {"label": "Siren", "actions": ON_OFF},
    "media_player": {"label": "Media player", "actions": [
        {"id": "turn_on", "label": "Turn on", "service": "turn_on"},
        {"id": "turn_off", "label": "Turn off", "service": "turn_off"},
        {"id": "media_pause", "label": "Pause", "service": "media_pause"},
        {"id": "media_play", "label": "Play", "service": "media_play"},
        {"id": "volume_set", "label": "Set volume", "service": "volume_set",
         "fields": [_field("volume_level", "Volume (0 to 1)", "number", min=0, max=1, step=0.01, required=True)]}]},
    "vacuum": {"label": "Vacuum", "actions": [
        {"id": "start", "label": "Start", "service": "start"},
        {"id": "return_to_base", "label": "Return to base", "service": "return_to_base"}]},
    "water_heater": {"label": "Water heater", "actions": [
        {"id": "set_temperature", "label": "Set temperature", "service": "set_temperature",
         "fields": [_field("temperature", "Temperature", "number", min=0, max=100, step=0.5, unit="°", required=True)]},
        {"id": "turn_on", "label": "Turn on", "service": "turn_on"},
        {"id": "turn_off", "label": "Turn off", "service": "turn_off"}]},
    "number": {"label": "Number", "actions": [
        {"id": "set_value", "label": "Set value", "service": "set_value",
         "fields": [_field("value", "Value", "number", step="any", required=True)]}]},
    "input_number": {"label": "Number helper", "actions": [
        {"id": "set_value", "label": "Set value", "service": "set_value",
         "fields": [_field("value", "Value", "number", step="any", required=True)]}]},
    "select": {"label": "Select", "actions": [
        {"id": "select_option", "label": "Select option", "service": "select_option",
         "fields": [_field("option", "Option", "option", attribute="options", required=True)]}]},
    "input_select": {"label": "Dropdown helper", "actions": [
        {"id": "select_option", "label": "Select option", "service": "select_option",
         "fields": [_field("option", "Option", "option", attribute="options", required=True)]}]},
    "button": {"label": "Button", "actions": [{"id": "press", "label": "Press", "service": "press"}]},
    "input_button": {"label": "Button helper", "actions": [{"id": "press", "label": "Press", "service": "press"}]},
    "scene": {"label": "Scene", "actions": [{"id": "turn_on", "label": "Activate", "service": "turn_on"}]},
    "script": {"label": "Script", "actions": [{"id": "turn_on", "label": "Run", "service": "turn_on"}]},
}
CONTROLLABLE_DOMAINS = frozenset(DOMAINS)

#: Domains whose turn_on / turn_off leave the entity in the state "on" / "off".
STATE_ON_OFF_DOMAINS = frozenset({"light", "switch", "input_boolean", "fan", "siren", "humidifier"})

# Effects ----------------------------------------------------------------------------------------------------
#: ``state``: the action leaves the entity in one of ``states`` (verifiable and safe to repeat).
#: ``idempotent``: repeating it changes nothing, but its result cannot be read from the entity's state.
#: ``non_idempotent``: every repetition does something again (toggle, press, start).
#: ``unknown``: not known to this App (hand-entered actions): treated like non-idempotent, never repeated.
NON_IDEMPOTENT_SERVICES = frozenset({
    "toggle", "press", "start", "increment", "decrement", "media_play_pause", "media_next_track",
    "media_previous_track", "volume_up", "volume_down", "trigger", "reload", "stop_cover", "media_play",
    "media_pause", "return_to_base", "select_next", "select_previous", "select_first", "select_last", "increase",
    "decrease",
})
IDEMPOTENT_SERVICES = frozenset({
    "set_temperature", "set_preset_mode", "set_cover_position", "set_cover_tilt_position", "set_percentage",
    "set_humidity", "volume_set", "set_value", "set_fan_mode", "set_swing_mode",
})


def effect(action: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The effect of ``domain.service`` with this data on its target entities."""
    domain, _, service = action.partition(".")
    data = data if isinstance(data, dict) else {}  # a generated rule's data can be one template
    if service in NON_IDEMPOTENT_SERVICES:
        return {"kind": "non_idempotent"}
    if domain in STATE_ON_OFF_DOMAINS and service in ("turn_on", "turn_off"):
        return {"kind": "state", "states": ["on" if service == "turn_on" else "off"]}
    if domain == "cover" and service in ("open_cover", "close_cover"):
        return {"kind": "state", "states": ["open", "opening"] if service == "open_cover" else ["closed", "closing"]}
    if domain == "valve" and service in ("open_valve", "close_valve"):
        return {"kind": "state", "states": ["open", "opening"] if service == "open_valve" else ["closed", "closing"]}
    if domain == "lock" and service in ("lock", "unlock"):
        return {"kind": "state", "states": ["locked", "locking"] if service == "lock" else ["unlocked", "unlocking"]}
    if domain == "climate" and service == "set_hvac_mode" and isinstance(data.get("hvac_mode"), str):
        return {"kind": "state", "states": [data["hvac_mode"]]}
    if domain == "climate" and service == "turn_off":
        return {"kind": "state", "states": ["off"]}
    if domain in ("select", "input_select") and service == "select_option" and isinstance(data.get("option"), str):
        return {"kind": "state", "states": [data["option"]]}
    if domain in ("media_player", "water_heater") and service == "turn_off":
        return {"kind": "state", "states": ["off"]}
    if service in IDEMPOTENT_SERVICES or service in ("turn_on", "turn_off") or domain == "scene":
        return {"kind": "idempotent"}
    return {"kind": "unknown"}


def catalog() -> dict[str, Any]:
    """For the interface: the controllable domains and their ready-made actions."""
    return {"domains": {d: {"label": v["label"], "actions": [
        {**a, "action": f"{d}.{a['service']}", "effect": effect(f"{d}.{a['service']}")["kind"]} for a in v["actions"]]}
        for d, v in DOMAINS.items()}}
