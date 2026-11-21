"""Phase 0.2 building blocks in Home Assistant's own engine: buttons and events, nested conditions, branches, waits
with timeouts, repeats, variables, notifications and scripts (Home Assistant 2026.11.1)."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

import builders as B

LAMP = "light.lamp"
LAMP2 = "light.lamp_2"
MOTION = "binary_sensor.motion"
REMOTE = "event.remote_action"
LUX = "sensor.lux"
DOOR = "binary_sensor.door"
ON = ("light.turn_on", (LAMP,), {})
OFF = ("light.turn_off", (LAMP,), {})


def press(house, event_type: str, stamp: str) -> None:
    """A Zigbee2MQTT / Matter style button: an event entity whose state is the time of the last event."""
    house.set(REMOTE, stamp, event_type=event_type, event_types=["single", "double", "hold"])


async def test_button_event_entity_single_double_long(house):
    """One remote, three presses, three rules - and becoming available is not a press."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Single", [B.button(REMOTE, "single")], [B.call("light.toggle")]),
        B.rule(2, "Double", [B.button(REMOTE, "double")], [B.call("light.turn_on", brightness_pct=100)]),
        B.rule(3, "Hold", [B.button(REMOTE, "hold")], [B.call("light.turn_off")]),
        B.rule(4, "Any", [B.button(REMOTE)], [B.call("light.turn_on", target=[LAMP], transition=1)], enabled=False),
    ])
    house.set(LAMP, "off")
    house.set(REMOTE, "unavailable")
    automations = await house.load(device)
    assert len(automations) == 1 and len(automations[0]["config"]["triggers"]) == 1  # one trigger, three rules
    press(house, "single", "2026-11-21T10:00:00.000+00:00")   # from unavailable: the remote came online
    await house.settle()
    assert house.taken() == []
    house.set(REMOTE, "unknown")                               # a button that has not reported anything yet ...
    await house.settle()
    press(house, "hold", "2026-11-21T10:00:02.000+00:00")     # ... its first event ever is a real press
    await house.settle()
    assert house.taken() == [OFF]
    press(house, "single", "2026-11-21T10:00:05.000+00:00")
    await house.settle()
    assert house.taken() == [("light.toggle", (LAMP,), {})]
    press(house, "double", "2026-11-21T10:00:07.000+00:00")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 100})]
    press(house, "hold", "2026-11-21T10:00:09.000+00:00")
    await house.settle()
    assert house.taken() == [OFF]
    press(house, "single", "2026-11-21T10:00:11.000+00:00")
    press(house, "single", "2026-11-21T10:00:12.000+00:00")  # the same press twice is two presses
    await house.settle()
    assert [c[0] for c in house.taken()] == ["light.toggle", "light.toggle"]
    house.set(REMOTE, "unavailable")
    await house.settle()
    assert house.taken() == []


async def test_event_trigger_zha_style(house, hass):
    """A ZHA remote without device triggers: its ``zha_event`` with the device and the command."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "On", [B.event("zha_event", device_ieee="00:11", command="on")], [B.call("light.turn_on")]),
        B.rule(2, "Off", [B.event("zha_event", device_ieee="00:11", command="off")], [B.call("light.turn_off")]),
    ])
    house.set(LAMP, "off")
    await house.load(device)
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "on", "args": []})
    await house.settle()
    assert house.taken() == [ON]
    hass.bus.async_fire("zha_event", {"device_ieee": "00:99", "command": "on"})  # another remote
    hass.bus.async_fire("other_event", {"device_ieee": "00:11", "command": "on"})
    await house.settle()
    assert house.taken() == []
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "off"})
    await house.settle()
    assert house.taken() == [OFF]


async def test_device_trigger_and_device_condition(house, hass):
    """A native device trigger and device condition, exactly as Home Assistant lists them for the device."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device_entry = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "btn")})
    registry = er.async_get(hass)
    sensor = registry.async_get_or_create("binary_sensor", "test", "btn1", device_id=device_entry.id)
    guard = registry.async_get_or_create("binary_sensor", "test", "guard", device_id=device_entry.id)
    hass.states.async_set(sensor.entity_id, "off")
    hass.states.async_set(guard.entity_id, "off")
    await async_setup_component(hass, "device_automation", {})
    await async_setup_component(hass, "binary_sensor", {})
    device = B.device(1, "Lamp", [LAMP], [B.rule(
        1, "Pressed",
        [B.device_trigger("Button turned on", device_id=device_entry.id, domain="binary_sensor", entity_id=sensor.id,
                          type="turned_on")],
        [B.call("light.turn_on")],
        B.all_of({"type": "device", "label": "Guard is on", "config": {
            "device_id": device_entry.id, "domain": "binary_sensor", "entity_id": guard.id, "type": "is_on"}}))])
    house.set(LAMP, "off")
    automations = await house.load(device)
    assert automations[0]["config"]["triggers"][0]["trigger"] == "device"
    hass.states.async_set(sensor.entity_id, "on")
    await house.settle()
    assert house.taken() == []  # the device condition does not hold
    hass.states.async_set(sensor.entity_id, "off")
    hass.states.async_set(guard.entity_id, "on")
    await house.settle()
    hass.states.async_set(sensor.entity_id, "on")
    await house.settle()
    assert house.taken() == [ON]


async def test_template_trigger_pattern_and_helper(house):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Dark and home", [B.template("{{ states('sensor.lux') | float(999) < 20 and is_state('person.a', 'home') }}",
                                               for_seconds=10)], [B.call("light.turn_on")]),
        B.rule(2, "Helper", [B.state("input_number.level")], [B.call("light.turn_on", brightness_pct=5)]),
        B.rule(3, "Attribute", [B.numeric("sun.sun", below=-6) | {"attribute": "elevation"}], [B.call("light.turn_off")]),
    ])
    house.set(LAMP, "off")
    house.set(LUX, "100")
    house.set("person.a", "home")
    house.set("input_number.level", "1")
    house.set("sun.sun", "above_horizon", elevation=10)
    await house.load(device)
    house.set(LUX, "10")
    await house.advance(8)
    assert house.taken() == []
    await house.advance(3)
    assert house.taken() == [ON]
    house.set("input_number.level", "2")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 5})]
    house.set("sun.sun", "below_horizon", elevation=-7)
    await house.settle()
    assert house.taken() == [OFF]


async def test_nested_and_or_not_template_and_duration_conditions(house):
    """IF motion AND (lux < 50 OR mood is Hygge) AND none of (guest mode, door open for 30 s) AND a template."""
    conditions = B.all_of(
        B.any_of(B.c_numeric(LUX, below=50), B.c_state("input_select.mood", "Hygge")),
        B.none_of(B.c_state("input_boolean.guest", "on"), B.c_state(DOOR, "on", for_seconds=30)),
        B.c_template("{{ states('sensor.people') | int(0) > 0 }}"))
    device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Motion", [B.state(MOTION, to="on")], [B.call("light.turn_on")], conditions)])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (LUX, "200"), ("input_select.mood", "Dagur"),
                          ("input_boolean.guest", "off"), (DOOR, "off"), ("sensor.people", "1")):
        house.set(entity, value)
    await house.load(device)

    async def motion() -> list:
        house.set(MOTION, "off")
        await house.settle()
        house.set(MOTION, "on")
        await house.settle()
        return house.taken()

    assert await motion() == []            # bright and not Hygge
    house.set("input_select.mood", "Hygge")
    assert await motion() == [ON]          # the OR group holds
    house.set("input_boolean.guest", "on")
    assert await motion() == []            # NOT group: guest mode
    house.set("input_boolean.guest", "off")
    house.set(DOOR, "on")
    assert await motion() == [ON]          # the door is open, but not for 30 s yet
    await house.advance(31)
    assert await motion() == []            # ... now it is
    house.set(DOOR, "off")
    house.set("sensor.people", "0")
    assert await motion() == []            # the template
    house.set("sensor.people", "2")
    house.set("input_select.mood", "Dagur")
    house.set(LUX, "10")
    assert await motion() == [ON]


async def test_choose_if_else_and_trigger_id_conditions(house):
    """One rule, two triggers: which one started it decides the branch; an if/else inside; a default branch."""
    device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Switch", [
        B.button(REMOTE, "single"), B.button(REMOTE, "double"), B.state(MOTION, to="on")], [
        B.choose(
            (B.all_of(B.c_trigger(0)), [B.if_(B.all_of(B.c_state(LAMP, "on")), [B.call("light.turn_off")],
                                              [B.call("light.turn_on", brightness_pct=50)])]),
            (B.all_of(B.c_trigger(1)), [B.call("light.turn_on", brightness_pct=100)]),
            default=[B.call("light.turn_on", brightness_pct=10)]),
    ])])
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    house.set(REMOTE, "2026-11-21T10:00:00.000+00:00", event_type="hold")
    automations = await house.load(device)
    assert len(automations) == 1 and automations[0]["mode"] == "queued"
    press(house, "single", "2026-11-21T10:00:05.000+00:00")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 50})]
    press(house, "single", "2026-11-21T10:00:06.000+00:00")
    await house.settle()
    assert house.taken() == [OFF]
    press(house, "double", "2026-11-21T10:00:07.000+00:00")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 100})]
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 10})]


async def test_wait_for_trigger_with_timeout(house, hass):
    """Motion: light on, wait until the motion ends (at most 60 s) - then off. A timeout that continues, one that
    stops, and the other rules of the device keep running meanwhile."""
    def build(on_timeout: str):
        return B.device(1, "Lamp", [LAMP, LAMP2], [
            B.rule(1, "Motion", [B.state(MOTION, to="on")], [
                B.call("light.turn_on", target=[LAMP]),
                B.wait_trigger(B.state(MOTION, to="off", for_seconds=5), timeout=60, on_timeout=on_timeout),
                B.call("light.turn_off", target=[LAMP])], retrigger="restart"),
            B.rule(2, "Door", [B.state(DOOR, to="on")], [B.call("light.turn_on", target=[LAMP2])]),
        ])

    for entity in (LAMP, LAMP2, MOTION, DOOR):
        house.set(entity, "off")
    automations = await house.load(build("continue"), {**build("stop"), "id": "d_00000002"})
    assert [(a["role"], a["mode"]) for a in automations] == [("shared", "queued"), ("separate", "restart")] * 2
    stop = automations[3]["config"]["actions"][0]["then"]
    assert stop[1]["continue_on_timeout"] is True and stop[2] == {
        "if": [{"condition": "template", "value_template": "{{ wait.completed }}"}],
        "then": [{"action": "light.turn_off", "target": {"entity_id": [LAMP]}}]}

    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {})] * 2
    house.set(DOOR, "on")                         # another rule of the same device, while the first waits
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP2,), {})] * 2
    house.set(MOTION, "off")
    await house.advance(4)
    assert house.taken() == []
    await house.advance(2)                        # the wait completed: both variants turn off
    assert house.taken() == [("light.turn_off", (LAMP,), {})] * 2

    house.set(MOTION, "on")
    await house.settle()
    assert len(house.taken()) == 2
    await house.advance(61)                       # nobody leaves: the timeout
    assert house.taken() == [("light.turn_off", (LAMP,), {})]   # "continue" goes on, "stop" does not
    for state in hass.states.async_all("automation"):
        assert state.attributes["current"] == 0, state  # no run is left hanging


async def test_wait_for_condition_and_repeat(house):
    """Wait until lux is high or the mood changes; blink three times; repeat until a state, with a pause."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Wait", [B.state(MOTION, to="on")], [
            B.wait_condition(B.any_of(B.c_numeric(LUX, above=100), B.c_state("input_select.mood", "Hygge", negate=True)),
                             timeout=300),
            B.call("light.turn_off")], retrigger="single"),
        B.rule(2, "Blink", [B.state(DOOR, to="on")], [
            B.repeat([B.call("light.toggle")], count=3)]),
        B.rule(3, "Until", [B.state("input_boolean.alarm", to="on")], [
            B.repeat([B.call("light.toggle"), B.delay(2), B.call("light.toggle")],
                     until=B.all_of(B.c_state("input_boolean.alarm", "off")))], retrigger="single"),
    ])
    for entity, value in ((LAMP, "on"), (MOTION, "off"), (LUX, "20"), ("input_select.mood", "Hygge"), (DOOR, "off"),
                          ("input_boolean.alarm", "off")):
        house.set(entity, value)
    automations = await house.load(device)
    assert [a["role"] for a in automations] == ["shared", "separate", "separate"]
    wait = automations[1]["config"]["actions"][0]["then"][0]
    assert wait["wait_template"] == ("{{ (((states('sensor.lux') | float(none)) is not none and (states('sensor.lux') "
                                     "| float(none)) > 100) or states('input_select.mood') != 'Hygge') }}")
    house.set(MOTION, "on")
    await house.advance(5)
    assert house.taken() == []
    house.set(LUX, "150")
    await house.settle()
    assert house.taken() == [OFF]

    house.set(DOOR, "on")
    await house.settle()
    assert [c[0] for c in house.taken()] == ["light.toggle"] * 3

    house.set("input_boolean.alarm", "on")
    await house.advance(5)
    assert len(house.taken()) >= 4
    house.set("input_boolean.alarm", "off")
    await house.advance(3)
    house.taken()
    await house.advance(6)
    assert house.taken() == []  # the loop ended


async def test_variables_templates_notification_and_script(house, hass):
    notes, runs = [], []

    async def note(call):
        notes.append(dict(call.data))

    async def bedtime(call):
        runs.append(dict(call.data))

    hass.services.async_register("notify", "phone", note)
    assert await async_setup_component(hass, "script", {"script": {
        "bedtime": {"sequence": [{"action": "test.record", "data": {"level": "{{ level }}"}}], "fields": {"level": {}}}}})
    hass.services.async_register("test", "record", bedtime)
    device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Dim by lux", [B.state(MOTION, to="on")], [
        B.variables(level="{{ [100 - (states('sensor.lux') | int(0)), 10] | max }}"),
        B.call("light.turn_on", brightness_pct="{{ level }}"),
        B.notify("notify.phone", "Lamp at {{ level }} % ({{ trigger.to_state.entity_id }})", title="Lamp"),
        B.script("script.bedtime", wait=False, level="{{ level }}"),
    ])])
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    house.set(LUX, "30")
    automations = await house.load(device)
    assert automations[0]["role"] == "shared"
    house.set(MOTION, "on")
    await hass.async_block_till_done()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 70})]
    assert notes == [{"message": "Lamp at 70 % (binary_sensor.motion)", "title": "Lamp"}]
    assert runs == [{"level": 70}]


async def test_execution_modes_of_waiting_rules(house, hass):
    """restart / single / queued / parallel are the rule's own choice and never touch the shared automation."""
    def rule(number: int, mode: str):
        return B.rule(number, f"Stairs {mode}", [B.state(f"binary_sensor.m_{mode}", to="on")],
                      [B.call("light.turn_on"), B.delay(10), B.call("light.turn_off")], retrigger=mode)

    modes = ("restart", "single", "queued", "parallel")
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(9, "Plain", [B.state(DOOR, to="on")], [B.call("light.turn_on", brightness_pct=1)]),
        *(rule(i + 1, mode) for i, mode in enumerate(modes))])
    house.set(LAMP, "off")
    house.set(DOOR, "off")
    for mode in modes:
        house.set(f"binary_sensor.m_{mode}", "off")
    automations = await house.load(device)
    assert [(a["role"], a["mode"]) for a in automations] == [("shared", "queued"), *(("separate", m) for m in modes)]

    async def twice(mode: str) -> list[str]:
        """Trigger at 0 s and at 5 s, then let 30 s pass: the actions that ran."""
        entity = f"binary_sensor.m_{mode}"
        house.set(entity, "on")
        await house.advance(5)
        house.set(entity, "off")
        await house.settle()
        house.set(entity, "on")
        await house.advance(30)
        house.set(entity, "off")
        await house.settle()
        return [c[0].split(".")[1] for c in house.taken()]

    assert await twice("restart") == ["turn_on", "turn_on", "turn_off"]                 # the wait started over
    assert await twice("single") == ["turn_on", "turn_off"]                              # the second was ignored
    assert await twice("queued") == ["turn_on", "turn_off", "turn_on", "turn_off"]       # one after the other
    assert await twice("parallel") == ["turn_on", "turn_on", "turn_off", "turn_off"]     # both at once
    house.set(f"binary_sensor.m_single", "on")   # while a rule waits, the plain rule answers at once
    await house.settle()
    house.set(DOOR, "on")
    await house.settle()
    assert house.taken() == [ON, ("light.turn_on", (LAMP,), {"brightness_pct": 1})]
    await house.advance(11)
