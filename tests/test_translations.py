"""Every UI string used by the flows and entities exists in strings.json."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

from custom_components.smart_occupancy import config_flow
from custom_components.smart_occupancy.const import (
    TIMING_PARAMETERS,
    LightAction,
    LightingMode,
    OccupancyState,
    OperatingMode,
    SensorRole,
)
from custom_components.smart_occupancy.coordinator import STATUS_OPTIONS
from custom_components.smart_occupancy.entity import (
    GLOBAL_BINARY_SENSOR_KEYS,
    GLOBAL_BUTTON_KEYS,
    GLOBAL_SENSOR_KEYS,
    ROOM_COMPARISON_ENTITY,
    ROOM_LIGHTING_ENTITY,
    ROOM_OCCUPANCY_ENTITIES,
)
from custom_components.smart_occupancy.sensor import COMPARISON_OPTIONS

ROOT = Path(config_flow.__file__).parent
STRINGS = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
SOURCE = (ROOT / "config_flow.py").read_text(encoding="utf-8")
PROFILES_SOURCE = (ROOT / "profiles.py").read_text(encoding="utf-8")


def _steps(cls) -> set[str]:
    return {n.removeprefix("async_step_") for n in dir(cls) if n.startswith("async_step_")}


def test_en_matches_strings() -> None:
    en = json.loads((ROOT / "translations" / "en.json").read_text(encoding="utf-8"))
    assert en == STRINGS


def test_all_steps_translated() -> None:
    own_config_steps = (_steps(config_flow.SmartOccupancyConfigFlow) - _steps(config_flow.ConfigFlow)) | {"user"}
    assert own_config_steps <= set(STRINGS["config"]["step"])
    own_option_steps = _steps(config_flow.SmartOccupancyOptionsFlow) - _steps(config_flow.OptionsFlow)
    assert own_option_steps <= set(STRINGS["options"]["step"])


def test_all_errors_and_aborts_translated() -> None:
    errors = set(re.findall(r'errors\[[^\]]+\] = "([a-z_]+)"', SOURCE))
    errors |= set(re.findall(r'errors\["[a-z_]+"\] = "([a-z_]+)"', PROFILES_SOURCE))
    aborts = set(re.findall(r'async_abort\(reason="([a-z_]+)"\)', SOURCE))
    assert errors, "regex found nothing"
    assert errors <= set(STRINGS["options"]["error"]) | set(STRINGS["config"]["error"])
    assert aborts <= set(STRINGS["options"]["abort"]) | set(STRINGS["config"]["abort"])


def test_menu_options_translated() -> None:
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "async_show_menu":
            kw = {k.arg: k.value for k in node.keywords}
            step = ast.literal_eval(kw["step_id"])
            options = ast.literal_eval(kw["menu_options"])
            assert set(options) == set(STRINGS["options"]["step"][step]["menu_options"])


def test_selectors_and_entities_translated() -> None:
    sel = STRINGS["selector"]
    assert set(sel["operating_mode"]["options"]) == {m.value for m in OperatingMode}
    assert set(sel["sensor_role"]["options"]) == {r.value for r in SensorRole}
    assert set(sel["light_action"]["options"]) == {a.value for a in LightAction}
    assert set(sel["profile_scope"]["options"]) == {"global", "room", "light"}
    room_keys = {tk for platform, tk in ROOM_OCCUPANCY_ENTITIES.values() if platform == "sensor"}
    room_keys |= {ROOM_COMPARISON_ENTITY[2], ROOM_LIGHTING_ENTITY[2], "room_observation"}
    assert set(GLOBAL_SENSOR_KEYS) | room_keys == set(STRINGS["entity"]["sensor"])
    room_binary = {tk for platform, tk in ROOM_OCCUPANCY_ENTITIES.values() if platform == "binary_sensor"}
    assert set(GLOBAL_BINARY_SENSOR_KEYS) | room_binary == set(STRINGS["entity"]["binary_sensor"])
    assert set(STRINGS["entity"]["sensor"]["room_state"]["state"]) == {s.value for s in OccupancyState}
    assert set(STRINGS["entity"]["sensor"]["room_helper_comparison"]["state"]) == set(COMPARISON_OPTIONS)
    assert set(STRINGS["entity"]["sensor"][ROOM_LIGHTING_ENTITY[2]]["state"]) == {m.value for m in LightingMode}
    assert set(sel["persistent"]["options"]) == {"inherit", "yes", "no"}
    timing_steps = (STRINGS["options"]["step"]["occupancy"], STRINGS["options"]["step"]["room_occupancy"])
    for step in timing_steps:
        assert set(TIMING_PARAMETERS) <= set(step["data"])
    assert set(GLOBAL_BUTTON_KEYS) == set(STRINGS["entity"]["button"])
    for key in ("status", "room_observation"):
        assert set(STRINGS["entity"]["sensor"][key]["state"]) == set(STATUS_OPTIONS)
