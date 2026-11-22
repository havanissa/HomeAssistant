"""The test automations of the end-to-end checks and their Home Assistant metadata (shared by prepare.py and the
check scripts). Every automation is created the way Home Assistant's own editor creates it (POST to the editor
endpoint) and organised through Home Assistant's own registry commands."""

from __future__ import annotations

from typing import Any

LABELS = {  # name -> definition
    "AIE Lighting": {"icon": "mdi:lightbulb", "color": "amber", "description": "Lights"},
    "AIE Security": {"icon": "mdi:shield-home", "color": "red"},
    "AIE Night": {"color": "#3f51b5"},
    "AIE Seasonal": {},
}
CATEGORIES = {"AIE Lights": "mdi:lightbulb-group", "AIE Safety": "mdi:shield", "AIE Comfort": None}
AREAS = ["AIE Kitchen", "AIE Hall", "AIE Bedroom"]

COMPLEX_ACTIONS = [
    {"variables": {"level": "{{ 3 if is_state('input_boolean.aie_guest', 'on') else 1 }}"}},
    {"choose": [{"conditions": [{"condition": "state", "entity_id": "input_boolean.aie_guest", "state": "on"}],
                 "sequence": [{"action": "script.aie_goodnight"}]}],
     "default": [{"action": "scene.turn_on", "target": {"entity_id": "scene.aie_evening"}}]},
    {"if": [{"condition": "template", "value_template": "{{ level | int > 2 }}"}],
     "then": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.aie_light"}}],
     "else": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.aie_light"}}]},
    {"parallel": [{"delay": {"milliseconds": 10}},
                  {"event": "aie_test_event", "event_data": {"source": "hall", "values": [1, 2.5, True, None]}}]},
    {"repeat": {"while": [{"condition": "state", "entity_id": "input_boolean.aie_motion", "state": "on"}],
                "sequence": [{"delay": {"seconds": 1}}]}},
    {"wait_for_trigger": [{"trigger": "state", "entity_id": "input_boolean.aie_motion", "to": "off"}],
     "timeout": "00:00:05", "continue_on_timeout": True},
    {"stop": "done"},
]

#: id -> (configuration, registry metadata, state). Metadata keys: labels, category, area, entity_id, name, icon,
#: aliases, hidden, disabled, expose.
AUTOMATIONS: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "aie_kitchen_motion": ({
        "alias": "AIE Kitchen motion light", "description": "Turns the kitchen light on with motion",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.aie_motion", "to": "on", "id": "motion"}],
        "conditions": [{"condition": "template", "value_template": "{{ states('sun.sun') == 'below_horizon' or true }}"}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.aie_lamp"}, "data": {}}],
        "mode": "restart", "variables": {"brightness": 80, "name": "kitchen"}, "trace": {"stored_traces": 7},
        "max_exceeded": "silent",
    }, {"labels": ["AIE Lighting", "AIE Night"], "category": "AIE Lights", "area": "AIE Kitchen",
        "entity_id": "automation.aie_kitchen_motion_custom", "name": "AIE Kitchen motion (custom name)",
        "icon": "mdi:motion-sensor", "aliases": ["kitchen motion"], "expose": True}, "on"),
    "aie_hall_alarm": ({
        "alias": "AIE Hall alarm", "description": "",
        "triggers": [{"trigger": "state", "entity_id": "input_boolean.aie_guest"},
                     {"trigger": "time_pattern", "minutes": "/30"}],
        "conditions": [], "actions": COMPLEX_ACTIONS, "mode": "queued", "max": 4,
    }, {"labels": ["AIE Security"], "category": "AIE Safety", "area": "AIE Hall"}, "off"),
    "aie_plain": ({
        "alias": "AIE Plain rule", "description": "No labels, no category, no area",
        "triggers": [{"trigger": "event", "event_type": "aie_plain"}], "conditions": [],
        "actions": [{"action": "input_boolean.toggle", "target": {"entity_id": "input_boolean.aie_target"}}],
        "mode": "single",
    }, {}, "on"),
    "aie_blueprint_motion": ({
        "alias": "AIE Bedroom motion (blueprint)", "description": "From Home Assistant's motion light blueprint",
        "use_blueprint": {"path": "homeassistant/motion_light.yaml",
                          "input": {"motion_entity": "binary_sensor.aie_motion",
                                    "light_target": {"entity_id": "light.aie_lamp"}, "no_motion_wait": 30}},
    }, {"labels": ["AIE Lighting"], "category": "AIE Lights", "area": "AIE Bedroom"}, "on"),
    "aie_custom_blueprint": ({
        "alias": "AIE Custom blueprint rule",
        "use_blueprint": {"path": "aie_test/toggle_on_change.yaml",
                          "input": {"source": "input_boolean.aie_guest", "target": "input_boolean.aie_target"}},
    }, {"labels": ["AIE Seasonal"]}, "on"),
    "aie_disabled": ({
        "alias": "AIE Disabled entity rule", "description": "Its entity is disabled in the registry",
        "triggers": [{"trigger": "event", "event_type": "aie_disabled"}], "conditions": [],
        "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.aie_target"}}],
        "mode": "single",
    }, {"labels": ["AIE Night"], "area": "AIE Bedroom", "disabled": True}, "off"),
    "aie_hidden": ({
        "alias": "AIE Hidden rule", "description": "Hidden, four labels",
        "triggers": [{"trigger": "event", "event_type": "aie_hidden"}], "conditions": [],
        "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.aie_target"}}],
        "mode": "parallel", "max": 3,
    }, {"labels": ["AIE Lighting", "AIE Security", "AIE Night", "AIE Seasonal"], "category": "AIE Comfort",
        "hidden": True}, "on"),
    "aie_unicode": ({
        "alias": "AIE Kvøld ljós 🌙 – nátt", "description": "Fleiri reglur\nwith a second line\n  and indentation",
        "triggers": [{"trigger": "time", "at": "21:30:00"}], "conditions": [{"condition": "time", "weekday": ["mon", "fri"]}],
        "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.aie_light"}},
                    {"action": "persistent_notification.create", "data": {"message": "on\nyes\n07:00 – \"quoted\" 'x'"}}],
        "mode": "single",
    }, {"labels": ["AIE Night"], "category": "AIE Comfort", "area": "AIE Bedroom"}, "on"),
    "1700000000123": ({
        "alias": "AIE Legacy syntax rule", "description": "Old singular keys (trigger/condition/action, platform, service)",
        "trigger": [{"platform": "state", "entity_id": "input_boolean.aie_motion", "to": "on"}],
        "condition": [], "action": [{"service": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.aie_light"}}],
        "mode": "single",
    }, {"category": "AIE Lights", "area": "AIE Kitchen"}, "on"),
    "aie_missing_dep": ({
        "alias": "AIE Missing dependency rule",
        "triggers": [{"trigger": "event", "event_type": "aie_missing"}], "conditions": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.aie_does_not_exist"}},
                    {"action": "script.aie_missing_script"}],
        "mode": "single",
    }, {}, "on"),
    # protected: Automation Builder's namespace
    "automation_builder_e2e_lamp": ({
        "alias": "AIE Lamp (Automation Builder)",
        "description": "Managed by the Automation Builder App: edit the rules there. Rules: Motion.",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.aie_motion", "to": "on"}], "conditions": [],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.aie_lamp"}}], "mode": "queued", "max": 20,
    }, {"labels": ["AIE Lighting"], "area": "AIE Kitchen"}, "on"),
    # ambiguous ownership: Builder's markers without its namespace
    "aie_copied_builder": ({
        "alias": "AIE Copied (Automation Builder)",
        "description": "Managed by the Automation Builder App: edit the rules there.",
        "triggers": [{"trigger": "event", "event_type": "aie_copied"}], "conditions": [],
        "actions": [{"action": "input_boolean.toggle", "target": {"entity_id": "input_boolean.aie_target"}}],
        "mode": "single",
    }, {"labels": ["AIE Seasonal"]}, "on"),
}

ORDINARY = [k for k in AUTOMATIONS if not k.startswith("automation_builder_") and k != "aie_copied_builder"]
BUILDER = ["automation_builder_e2e_lamp"]
REVIEW = ["aie_copied_builder"]
EXTERNAL = ["package_rule", "legacy_with_id"]
