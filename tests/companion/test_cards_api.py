"""What the dashboard cards and Home Assistant events need: the view subscription, relayed actions, renames, recheck."""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.smart_occupancy import protocol as P

from .conftest import FakeApp, occupancy_view, setup_companion, system_view, user_ws


async def test_actions_are_relayed_to_the_app_and_answered(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting", "curtains")
    card = FakeApp(hass, await hass_ws_client(hass))  # a browser (administrator) with a dashboard card
    task = asyncio.ensure_future(card.send(type="smart_occupancy/companion/action", kind="room/resume",
                                           params={"area_id": "kamar_nordur"}))
    action = await app.event("action")
    assert action["kind"] == "room/resume" and action["params"] == {"area_id": "kamar_nordur"}
    reply = await app.send(type="smart_occupancy/companion/action_result", session_id=app.session,
                           action_id=action["action_id"], ok=True, result={"resumed": True})
    assert reply["success"] and reply["result"]["accepted"] is True
    answer = await task
    assert answer["success"] and answer["result"] == {"resumed": True}
    # A refusal of the App reaches the card with its code.
    task = asyncio.ensure_future(card.send(type="smart_occupancy/companion/action", kind="curtains/apply",
                                           params={"window_id": "w1", "preset_id": "open"}))
    action = await app.event("action")
    assert action["kind"] == "curtains/apply"
    await app.send(type="smart_occupancy/companion/action_result", session_id=app.session,
                   action_id=action["action_id"], ok=False, error={"code": "moving", "message": "a cover is moving"})
    answer = await task
    assert not answer["success"] and answer["error"]["code"] == "moving"
    # Without the App: refused at once.
    await app.client.close()
    await hass.async_block_till_done()
    answer = await card.send(type="smart_occupancy/companion/action", kind="room/resume", params={})
    assert not answer["success"] and answer["error"]["code"] == P.REJECT_NOT_OWNER


async def test_view_subscription_pushes_changes(hass: HomeAssistant, hass_ws_client, hass_read_only_user, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy")
    viewer = FakeApp(hass, await user_ws(hass, hass_ws_client, hass_read_only_user))
    viewer._id += 1
    viewer.sub_id = viewer._id
    await viewer.client.send_json({"id": viewer.sub_id, "type": "smart_occupancy/companion/view/subscribe"})
    assert (await viewer.client.receive_json())["success"]
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    events = await viewer.drain()
    assert {e["changed"] for e in events} >= {"occupancy", "rooms"}
    view = (await viewer.send(type="smart_occupancy/companion/view"))["result"]
    assert view["rooms"]["kamar_nordur"]["name"] == "Kamar Norður"


async def test_renames_reach_the_app_and_wait_for_it(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    reg = er.async_get(hass)
    created = reg.async_get_or_create("light", "test", "uid_lamp", suggested_object_id="kamar_lamp")
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    reg.async_update_entity(created.entity_id, new_entity_id="light.kamar_lamp_new")
    await hass.async_block_till_done()
    events = await app.drain()
    assert [e["renames"] for e in events if e.get("type") == "renames"] == [[["light.kamar_lamp", "light.kamar_lamp_new"]]]
    # The App is away: the rename is kept and handed to the next session's hello.
    await app.client.close()
    await hass.async_block_till_done()
    reg.async_update_entity("light.kamar_lamp_new", new_entity_id="light.kamar_lamp_newer")
    await hass.async_block_till_done()
    again = FakeApp(hass, await hass_ws_client(hass), instance=app.instance)
    again.pairing_token = app.pairing_token
    hello = await again.subscribe()
    assert hello["renames"] == [["light.kamar_lamp", "light.kamar_lamp_new"], ["light.kamar_lamp_new", "light.kamar_lamp_newer"]]
    assert (await again.send(type="smart_occupancy/companion/renames_applied", session_id=again.session))["success"]
    hello = await FakeApp(hass, await hass_ws_client(hass)).subscribe(token=None)
    assert hello["renames"] == []


async def test_recheck_button_asks_the_app(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy")
    await hass.services.async_call("button", "press", {"entity_id": "button.smart_occupancy_lighting_re_check_diagnostics"},
                                   blocking=True)
    events = await app.drain()
    assert any(e.get("type") == "recheck" for e in events)
