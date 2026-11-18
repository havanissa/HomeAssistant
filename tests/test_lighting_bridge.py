"""Phase A4: lighting ownership, the fenced executor, handover, mirror, fallback and rollback (integration side).

A simulated App (``FakeApp``) speaks the lighting bridge protocol over a real
websocket connection. The tests prove:

* the integration controls lighting by default (also after a restart); a
  session gets an exact copy (configuration, state, options) and every command,
  decision and configuration change of the integration's controller;
* a reviewed handover needs the same lighting code, the current configuration,
  readiness and the expected epoch; it stops the integration's controller (dormant,
  fenced), sends no command and hands over configuration and state;
* while the App controls lighting the integration's lighting editors and options
  steps are read-only, reading still works, and the room entities show the App's
  published view;
* the App's commands are executed only from the owner's session with the current
  epoch, the lease token and an increasing sequence number, and only for an
  allowlisted service of a configured target with allowed data; everything else is
  refused and changes nothing; the App's context is used, so its reports are its own;
* the mirror (configuration and state) and the occupancy inputs reach the
  integration;
* release, the ``return`` command, the admin service, lease expiry and a new App
  instance give control back: the integration's controller restarts from the App's
  last configuration and state (manual overrides kept), sends nothing at the switch,
  and later App commands are refused;
* Home Assistant restarts and integration reloads keep the controller dormant;
  only the same App process may resume; otherwise the integration takes over after
  the grace period;
* non-administrators cannot use any bridge command or the service.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from custom_components.smart_occupancy import lighting_sync as S
from custom_components.smart_occupancy.const import DOMAIN
from custom_components.smart_occupancy.occupancy_core import fingerprint as core_fingerprint
from custom_components.smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_lighting_runtime import WS_LOGGER, lit, options, set_moods  # noqa: F401

SUB = "smart_occupancy/lighting/bridge/subscribe"
TIMING = {"correlation_window": 10, "exit_confirmation": 30, "inactivity_timeout": 60, "startup_hold": 0}
START = "2026-06-15 10:00:00+00:00"


@pytest.fixture
def devices(hass: HomeAssistant) -> lp.Devices:
    return lp.Devices(hass)


def readiness(ready: bool = True) -> dict[str, Any]:
    return {"ready": ready, "checks": [{"key": "commands", "status": "ok" if ready else "error"}],
            "summary": {"seconds": 7200, "matched": 20, "mismatches": 0}}


class FakeApp:
    """The App's side of the lighting bridge on one websocket connection."""

    def __init__(self, hass: HomeAssistant, client: Any, instance: str | None = None) -> None:
        self.hass = hass
        self.client = client
        self.instance = instance or secrets.token_hex(16)
        self._id = 100
        self.sub_id: int | None = None
        self.events: list[dict[str, Any]] = []
        self.session: str | None = None
        self.hello: dict[str, Any] | None = None
        self.epoch: int | None = None
        self.token: str | None = None
        self.seq = 0

    async def send(self, **msg: Any) -> dict[str, Any]:
        self._id += 1
        await self.client.send_json({"id": self._id, **msg})
        while True:
            reply = await self.client.receive_json()
            if reply.get("type") == "event" and reply.get("id") == self.sub_id:
                self.events.append(reply["event"])
                continue
            if reply.get("id") == self._id:
                return reply

    async def drain(self) -> list[dict[str, Any]]:
        await self.hass.async_block_till_done()
        await self.send(type="ping")
        return self.events

    async def connect(self, resume_token: str | None = None, *, fp: str | None = None,
                      expect_ok: bool = True) -> dict[str, Any]:
        self._id += 1
        self.sub_id = self._id
        await self.client.send_json({
            "id": self._id, "type": SUB, "instance_id": self.instance, "protocol": S.PROTOCOL_VERSION,
            "app_version": "0.5.0", "runtime_version": RUNTIME_VERSION, "fingerprint": fp or fingerprint(),
            "core_fingerprint": core_fingerprint(), "resume_token": resume_token})
        reply = await self.client.receive_json()
        if not expect_ok:
            return reply
        assert reply["success"], reply
        hello = (await self.client.receive_json())["event"]
        assert hello["type"] == "hello"
        self.hello, self.session = hello, hello["session_id"]
        if hello.get("lease_token"):
            self.token, self.epoch, self.seq = hello["lease_token"], hello["ownership"]["epoch"], 0
        return hello

    async def claim(self, *, ready: bool = True, epoch: int | None = None, revision: str | None = None) -> dict:
        reply = await self.send(type="smart_occupancy/lighting/bridge/claim", session_id=self.session,
                                expected_epoch=self.hello["ownership"]["epoch"] if epoch is None else epoch,
                                config_revision=revision or self.hello["config_revision"], readiness=readiness(ready))
        if reply["success"]:
            self.epoch, self.token, self.seq = reply["result"]["epoch"], reply["result"]["lease_token"], 0
        return reply

    def owner(self, **over: Any) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session, "epoch": self.epoch, "lease_token": self.token, "seq": self.seq, **over}

    async def execute(self, domain: str, service: str, data: dict[str, Any], *, context: str | None = None,
                      **over: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/lighting/bridge/execute", **self.owner(**over), domain=domain,
                               service=service, data=data, context_id=context or Context().id)

    async def heartbeat(self, **over: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/lighting/bridge/heartbeat", **self.owner(**over))

    async def publish(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/lighting/bridge/publish", **self.owner(), **params)

    async def inputs(self, kind: str, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/lighting/bridge/inputs", **self.owner(), kind=kind, **params)

    async def release(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/lighting/bridge/release", **self.owner(), **params)


async def admin_client(hass, hass_ws_client, hass_admin_user):
    return await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/")))


async def tick(hass: HomeAssistant, freezer, seconds: float, step: float = 1.0) -> None:
    await lp.advance(hass, freezer, seconds, step=step)


async def alive(hass: HomeAssistant, freezer, app: FakeApp, seconds: float) -> None:
    """Time passes while the App keeps its lease."""
    while seconds > 0:
        step = min(S.HEARTBEAT_SECONDS, seconds)
        await tick(hass, freezer, step)
        assert (await app.heartbeat())["success"]
        seconds -= step


async def started(hass, lit, freezer, **room: Any):
    freezer.move_to(START)
    entry = await setup_entry(hass, options(lit, timing=TIMING))
    ctrl = entry.runtime_data.lighting
    ctrl.store.async_set(ctrl.with_room(lit.area, control_enabled=True, **room))
    await tick(hass, freezer, 5)
    return entry


def bridge_of(entry):
    return entry.runtime_data.lighting_bridge


async def occupy(hass, lit, freezer) -> None:
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await tick(hass, freezer, 12)


# ------------------------------------------------------------------ default and shadow
async def test_default_owner_and_shadow_copy(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                             hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    assert bridge_of(entry).owner == S.OWNER_INTEGRATION and not entry.runtime_data.lighting.dormant
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    hello = await app.connect()
    ctrl = entry.runtime_data.lighting
    assert hello["config"] == ctrl.lighting.as_dict()
    assert hello["config_revision"] == S.config_revision(ctrl.lighting.as_dict())
    assert hello["state"] == ctrl.export_state() and hello["options"] == dict(entry.options)
    assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["lease_token"] is None
    await occupy(hass, lit, freezer)
    events = await app.drain()
    commands = [e["command"] for e in events if e["type"] == "command"]
    assert commands and all(c["purpose"] == "lighting" and c["domain"] == "light" for c in commands)
    assert any(e["type"] == "decision" and e["subject"] == lit.area for e in events)
    ctrl.store.async_set(ctrl.with_room(lit.area, manual_override_minutes=7))
    events = await app.drain()
    config = [e for e in events if e["type"] == "config"][-1]
    assert config["config"]["rooms"][lit.area]["manual_override_minutes"] == 7


async def test_handover_requires_matching_code_config_and_readiness(hass: HomeAssistant, lit, devices, freezer,
                                                                    hass_ws_client, hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    client = await admin_client(hass, hass_ws_client, hass_admin_user)
    app = FakeApp(hass, client)
    await app.connect(fp="0" * 64)
    assert (await app.claim())["error"]["code"] == S.REJECT_VERSION
    app2 = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app2.connect()
    assert (await app2.claim(ready=False))["error"]["code"] == S.REJECT_NOT_READY
    assert (await app2.claim(revision="0" * 16))["error"]["code"] == S.REJECT_CONFIG
    assert (await app2.claim(epoch=99))["error"]["code"] == S.REJECT_BUSY
    assert bridge_of(entry).owner == S.OWNER_INTEGRATION and not entry.runtime_data.lighting.dormant


# ------------------------------------------------------------------ handover and the owner
async def test_handover_stops_the_integration_controller_and_sends_nothing(
        hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    await occupy(hass, lit, freezer)
    old = entry.runtime_data.lighting
    # A person's change: the manual override must survive the handover.
    state = hass.states.get(lit.lamp)
    hass.states.async_set(lit.lamp, "on", {**state.attributes, "brightness": 20},
                          context=Context(user_id=hass_admin_user.id))
    await tick(hass, freezer, 2)
    assert lit.lamp in old.runtime[lit.area].overrides
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    devices.clear()
    reply = await app.claim()
    assert reply["success"], reply
    grant = reply["result"]
    assert grant["state"]["rooms"][lit.area]["overrides"][lit.lamp]["source"] == "ui"
    assert grant["config"] == old.lighting.as_dict()
    ctrl = entry.runtime_data.lighting
    assert ctrl is old and ctrl.dormant and ctrl.fenced and ctrl._stopped  # noqa: SLF001
    # Nothing happens on its own any more: a mood change sends nothing from the integration.
    set_moods(hass, "Hygge", "Hygge", "Hygge")
    await alive(hass, freezer, app, 10)
    assert devices.calls == []
    status = bridge_of(entry).status()
    assert status["owner"] == S.OWNER_APP and status["controller"] == "dormant"
    assert status["history"][0]["reason"] == S.REASON_HANDOVER
    # The integration's controller cannot send even when asked directly.
    with pytest.raises(Exception, match="moving to another controller"):
        await ctrl.async_call_service("light", "turn_on", {"entity_id": [lit.lamp]}, Context(), area_id=lit.area,
                                      purpose="lighting")


async def test_owner_commands_are_fenced_and_allowlisted(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                                         hass_admin_user, caplog) -> None:
    entry = await started(hass, lit, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    assert (await app.claim())["success"]
    context = Context().id
    reply = await app.execute("light", "turn_on", {"entity_id": [lit.lamp], "brightness_pct": 40,
                                                   "color_temp_kelvin": 2700, "transition": 1}, context=context)
    assert reply["success"], reply
    assert hass.states.get(lit.lamp).state == "on"
    assert devices.calls[-1].context.id == context  # the App's own context (its reports are its own)
    refused = {
        "domain": await app.execute("lock", "unlock", {"entity_id": ["lock.front_door"]}),
        "service": await app.execute("light", "reload", {"entity_id": [lit.lamp]}),
        "unknown light": await app.execute("light", "turn_on", {"entity_id": ["light.somewhere_else"]}),
        "data": await app.execute("light", "turn_on", {"entity_id": [lit.lamp], "white_value": 3, "x": 1}),
        "mixed domains": await app.execute("light", "turn_on", {"entity_id": [lit.lamp, "switch.boiler"]}),
        "relay not configured": await app.execute("switch", "turn_on", {"entity_id": ["switch.boiler"]}),
        "scene not mapped": await app.execute("scene", "turn_on", {"entity_id": ["scene.party"]}),
    }
    for what, r in refused.items():
        assert not r["success"] and r["error"]["code"] == S.REJECT_NOT_ALLOWED, (what, r)
    # Fencing: a stale epoch, a wrong lease, a replayed or older sequence, a foreign session.
    assert (await app.execute("light", "turn_on", {"entity_id": [lit.desk]}, epoch=app.epoch - 1))["error"][
        "code"] == S.REJECT_STALE_EPOCH
    assert (await app.execute("light", "turn_on", {"entity_id": [lit.desk]}, lease_token="f" * 32))["error"][
        "code"] == S.REJECT_LEASE
    seq = app.seq
    assert (await app.execute("light", "turn_on", {"entity_id": [lit.desk]}, seq=seq - 3))["error"][
        "code"] == S.REJECT_SEQUENCE
    other = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user), instance=app.instance)
    other.session, other.epoch, other.token, other.seq = app.session, app.epoch, app.token, app.seq + 100
    assert (await other.execute("light", "turn_on", {"entity_id": [lit.desk]}))["error"][
        "code"] == S.REJECT_UNKNOWN_SESSION
    assert hass.states.get(lit.desk).state == "off"
    assert bridge_of(entry).rejections[S.REJECT_NOT_ALLOWED] == len(refused)


async def test_editors_are_read_only_while_the_app_controls_lighting(
        hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    client = await admin_client(hass, hass_ws_client, hass_admin_user)
    app = FakeApp(hass, client)
    await app.connect()
    assert (await app.claim())["success"]
    editor = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    model = await editor.send(type="smart_occupancy/lighting/editor", area_id=lit.area)
    assert model["success"]  # reading keeps working
    for msg in ({"type": "smart_occupancy/lighting/settings/set", "manual_override_minutes": 5},
                {"type": "smart_occupancy/lighting/room/deactivate", "area_id": lit.area},
                {"type": "smart_occupancy/lighting/profile/test", "area_id": lit.area, "mood": "Dagur",
                 "profile": "occupied", "confirm": True}):
        reply = await editor.send(**msg)
        assert not reply["success"] and reply["error"]["code"] == "managed_by_app", (msg, reply)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("profiles", "categories"):
        result = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": step})
        assert result["type"] == "abort" and result["reason"] == "managed_by_app"
        flow = await hass.config_entries.options.async_init(entry.entry_id)


async def test_mirror_view_and_inputs(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                      hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    grant = (await app.claim())["result"]
    doc = dict(grant["config"])
    doc["manual_override_minutes"] = 11
    assert (await app.publish(config=doc, config_revision="0" * 16))["error"]["code"] == S.REJECT_CONFIG
    reply = await app.publish(config=doc, config_revision=S.config_revision(doc), state=grant["state"])
    assert reply["success"], reply
    ctrl = entry.runtime_data.lighting
    assert ctrl.lighting.manual_override_minutes == 11 and bridge_of(entry).mirror["state"] == grant["state"]
    assert (await app.publish(state={"bogus": 1}))["error"]["code"] == S.REJECT_INVALID
    # The room entity shows what the App published.
    view = {"active": True, "diagnostics": ctrl.room_diagnostics(lit.area),
            "daylight": ctrl.daylight.status(lit.area).summary(), "activation": {"at": None, "text": "From the App"}}
    assert (await app.publish(rooms={lit.area: view}, manual_mode=False))["success"]
    await hass.async_block_till_done()
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{lit.area}_daylight")
    assert hass.states.get(sensor).attributes["activation"] == "From the App"
    # Occupancy inputs of the App's lighting reach the integration's engine.
    occupancy = entry.runtime_data.occupancy
    assert (await app.inputs("preliminary", signals={lit.area: [lit.pir]}))["success"]
    assert occupancy.engines[lit.area].preliminary == frozenset({lit.pir})
    assert (await app.inputs("hold", area_id=lit.area, zones=["Desk"]))["success"]
    assert occupancy.engines[lit.area].external_hold == frozenset({"Desk"})
    assert (await app.inputs("activity", area_id=lit.area, source="wall switch"))["success"]


# ------------------------------------------------------------------ giving control back
@pytest.mark.parametrize("how", ["release", "return", "service", "lease", "new_instance"])
async def test_control_returns_with_the_apps_state_and_nothing_is_switched(
        hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user, how: str) -> None:
    entry = await started(hass, lit, freezer)
    await occupy(hass, lit, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    grant = (await app.claim())["result"]
    # The App's controller records a manual override and changes the configuration; it mirrors both.
    state = grant["state"]
    state["rooms"][lit.area]["overrides"] = {lit.desk: {
        "entity_id": lit.desk, "source": "ui", "since": "2026-06-15T10:00:20+00:00",
        "until": "2026-06-15T11:00:20+00:00", "user_id": hass_admin_user.id, "state": "on"}}
    doc = dict(grant["config"])
    doc["manual_override_minutes"] = 45
    assert (await app.publish(config=doc, config_revision=S.config_revision(doc), state=state))["success"]
    await alive(hass, freezer, app, 6)
    devices.clear()
    bridge = bridge_of(entry)
    if how == "release":
        assert (await app.release(detail="test"))["success"]
    elif how == "return":
        reply = await app.send(type="smart_occupancy/lighting/bridge/return", reason="test")
        assert reply["success"] and reply["result"]["returned"]
    elif how == "service":
        await hass.services.async_call(DOMAIN, "lighting_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_admin_user.id))
    elif how == "lease":
        await tick(hass, freezer, S.LEASE_SECONDS + 2)
    else:
        newcomer = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
        hello = await newcomer.connect()
        assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["state"]["rooms"][lit.area][
            "overrides"][lit.desk]["source"] == "ui"  # its copy is the restarted controller's
    await tick(hass, freezer, 5)
    ctrl = entry.runtime_data.lighting
    assert bridge.owner == S.OWNER_INTEGRATION and not ctrl.dormant and not ctrl.fenced
    assert lit.desk in ctrl.runtime[lit.area].overrides  # the App's manual override is kept
    assert ctrl.lighting.manual_override_minutes == 45  # and its configuration
    assert devices.calls == []  # taking control back switches nothing
    # The App is fenced out; the integration's controller works again (a mood change applies).
    assert (await app.execute("light", "turn_on", {"entity_id": [lit.lamp]}))["error"]["code"] in S.LOST_CODES
    set_moods(hass, "Hygge", "Hygge", "Hygge")
    await tick(hass, freezer, 5)
    assert devices.calls and lit.desk not in {e for c in devices.calls for e in devices.ids(c)}
    assert bridge.history[-1]["owner"] == S.OWNER_INTEGRATION


async def test_restart_keeps_the_controller_dormant_and_only_the_same_app_resumes(
        hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    assert (await app.claim())["success"]
    token = app.token
    await bridge_of(entry).async_flush()
    devices.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bridge = bridge_of(entry)
    ctrl = entry.runtime_data.lighting
    assert bridge.owner == S.OWNER_APP and bridge.resume_pending and ctrl.dormant and not ctrl._running  # noqa: SLF001
    set_moods(hass, "Hygge", "Hygge", "Hygge")
    await tick(hass, freezer, 10)
    assert devices.calls == []  # the restarted integration does not take lights back on its own
    again = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user), instance=app.instance)
    hello = await again.connect(resume_token=token)
    assert hello["lease_token"] and hello["ownership"]["owner"] == S.OWNER_APP
    assert hello["ownership"]["history"][0]["reason"] == S.REASON_RESUMED
    assert (await again.execute("light", "turn_on", {"entity_id": [lit.lamp]}))["success"]
    # A second reload, and this time nobody comes back: the integration takes over after the grace period.
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await tick(hass, freezer, S.RESUME_GRACE_SECONDS + 3, step=3)
    bridge = bridge_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and bridge.history[-1]["reason"] == S.REASON_NOT_RESUMED
    assert not entry.runtime_data.lighting.dormant


async def test_claim_waits_for_commands_in_flight(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                                  hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    ctrl = entry.runtime_data.lighting
    ctrl._inflight, ctrl._idle = 1, asyncio.Event()  # noqa: SLF001 - a command being sent
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    await app.connect()
    task = asyncio.ensure_future(app.claim())  # (not one of Home Assistant's tasks: time must move meanwhile)
    for _ in range(int(S.DRAIN_SECONDS) + 10):  # (time is frozen: the drain's timeout fires when it moves)
        await tick(hass, freezer, 1)
        if task.done():
            break
    reply = await task
    assert reply["error"]["code"] == S.REJECT_BUSY and not ctrl.fenced and bridge_of(entry).owner == S.OWNER_INTEGRATION


async def test_options_from_the_owner(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                      hass_admin_user) -> None:
    entry = await started(hass, lit, freezer)
    app = FakeApp(hass, await admin_client(hass, hass_ws_client, hass_admin_user))
    hello = await app.connect()
    assert (await app.claim())["success"]
    mood = {**hello["options"]["mood"], "main_mood_entity": "input_select.main_mood"}
    stale = await app.send(type="smart_occupancy/lighting/bridge/options", **app.owner(), options_revision="0" * 16,
                           mood=mood)
    assert stale["error"]["code"] == S.REJECT_CONFIG
    categories = [c for c in hello["options"]["categories"] if c["key"] != "decor"]
    reply = await app.send(type="smart_occupancy/lighting/bridge/options", **app.owner(),
                           options_revision=S.config_revision(hello["options"]), categories=categories)
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert [c["key"] for c in entry.options["categories"]] == [c["key"] for c in categories]
    assert entry.options["rooms"] == hello["options"]["rooms"]  # nothing else changed


async def test_non_admins_cannot_use_the_bridge(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                                hass_read_only_user, caplog) -> None:
    caplog.set_level("CRITICAL", logger=WS_LOGGER)
    entry = await started(hass, lit, freezer)
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/")))
    app = FakeApp(hass, client)
    reply = await app.connect(expect_ok=False)
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    for kind in ("status", "return"):
        reply = await app.send(type=f"smart_occupancy/lighting/bridge/{kind}")
        assert reply["error"]["code"] == "unauthorized"
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "lighting_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert bridge_of(entry).owner == S.OWNER_INTEGRATION

