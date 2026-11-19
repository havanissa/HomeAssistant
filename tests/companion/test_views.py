"""Published views on the native entities: states, attributes, availability, freshness, validation."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.smart_occupancy import protocol as P

from .conftest import (
    observation,
    KAMAR,
    FakeApp,
    curtains_view,
    entity_view,
    forecast_view,
    lighting_view,
    occupancy_view,
    setup_companion,
    system_view,
)


async def test_every_published_view_reaches_its_entities(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "lighting", "curtains", "forecast")
    for domain, view in (("system", system_view()), ("occupancy", occupancy_view()), ("lighting", lighting_view()),
                         ("forecast", forecast_view()), ("curtains", curtains_view())):
        reply = await app.publish(domain, view)
        assert reply["success"], (domain, reply)
    await hass.async_block_till_done()
    s = hass.states
    assert s.get("sensor.smart_occupancy_lighting_status").state == "healthy"
    assert s.get("sensor.smart_occupancy_lighting_status").attributes["companion"]["app_connected"] is True
    assert s.get("sensor.smart_occupancy_lighting_operating_mode").state == "observe"
    assert s.get("sensor.smart_occupancy_lighting_effective_mood").state == "Dagur"
    assert s.get("sensor.smart_occupancy_lighting_configured_rooms").state == "1"
    assert s.get("binary_sensor.smart_occupancy_lighting_mood_mismatch").state == "off"
    assert observation(hass, KAMAR).state == "healthy"
    state = s.get("sensor.smart_occupancy_kamar_nordur_state")
    assert state.state == "confirmed_occupancy" and state.attributes["integration"] == "smart_occupancy"
    assert state.attributes["area_id"] == KAMAR and state.attributes["decided_by"] == "app"
    assert state.attributes["entities"]["occupied"] == "binary_sensor.smart_occupancy_kamar_nordur_occupied"
    assert s.get("sensor.smart_occupancy_kamar_nordur_confidence").state == "80"
    assert s.get("sensor.smart_occupancy_kamar_nordur_last_activity").state == "2026-11-19T10:00:00+00:00"
    assert s.get("sensor.smart_occupancy_kamar_nordur_reason").state == "PIR detected motion"
    assert s.get("sensor.smart_occupancy_kamar_nordur_helper_comparison").state == "agree"
    assert s.get("binary_sensor.smart_occupancy_kamar_nordur_occupied").state == "on"
    lighting = s.get("sensor.smart_occupancy_kamar_nordur_mood_lighting")
    assert lighting.state == "active" and lighting.attributes["integration"] == "smart_occupancy"
    assert s.get("sensor.smart_occupancy_kamar_nordur_daylight").state == "dark"
    assert s.get("sensor.smart_occupancy_kamar_nordur_forecast").state == "62"
    assert s.get("sensor.smart_occupancy_kamar_nordur_forecast_confidence").state == "high"
    assert s.get("binary_sensor.smart_occupancy_kamar_nordur_predicted").state == "on"
    assert s.get("sensor.smart_occupancy_lighting_predicted_next_room").state == "Stova"
    assert s.get("sensor.smart_occupancy_lighting_forecast_learning").state == "ready"
    # The cards' view carries everything, including the curtain windows (which have no entity).
    view = (await app.send(type="smart_occupancy/companion/view"))["result"]
    assert view["curtains"]["windows"]["w1"]["mode"] == "automatic" and view["app_connected"] is True
    assert view["owners"] == {d: P.STATE_APP for d in P.DOMAINS}
    assert view["occupancy"][KAMAR]["state"]["value"] == "confirmed_occupancy"


async def test_availability_and_unknown_values(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "forecast")
    assert (await app.publish("system", system_view()))["success"]
    view = forecast_view()
    view["rooms"][KAMAR]["forecast"] = entity_view(None, {"status": "insufficient_data"}, available=True)
    view["rooms"][KAMAR]["predicted"] = entity_view(None, {}, available=False)
    assert (await app.publish("forecast", view))["success"]
    occ = occupancy_view()
    occ["rooms"][KAMAR]["state"] = entity_view("dancing")  # not an occupancy state: unknown, never an error
    occ["rooms"][KAMAR]["last_activity"] = entity_view("yesterday")
    assert (await app.publish("occupancy", occ))["success"]
    await hass.async_block_till_done()
    s = hass.states
    assert s.get("sensor.smart_occupancy_kamar_nordur_forecast").state == "unknown"
    assert s.get("binary_sensor.smart_occupancy_kamar_nordur_predicted").state == "unavailable"
    assert s.get("sensor.smart_occupancy_kamar_nordur_state").state == "unknown"
    assert s.get("sensor.smart_occupancy_kamar_nordur_last_activity").state == "unknown"
    # Never published lighting: unavailable (nothing is invented).
    assert s.get("sensor.smart_occupancy_kamar_nordur_mood_lighting").state == "unavailable"


async def test_invalid_or_foreign_views_are_refused(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy")
    assert (await app.publish("system", system_view()))["success"]
    bad = occupancy_view()
    bad["rooms"][KAMAR]["bogus"] = entity_view(1)
    reply = await app.publish("occupancy", bad)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_INVALID
    reply = await app.publish("occupancy", {"rooms": {"not valid!": {}}})
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_INVALID
    # Not the owner of lighting: refused, and the entities stay unavailable.
    app.grants["lighting"] = {"epoch": 0, "lease_token": "a" * 32}
    reply = await app.publish("lighting", lighting_view())
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_OWNER
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_mood_lighting").state == "unavailable"
    # The system view needs a session that owns a domain.
    other = FakeApp(hass, await hass_ws_client(hass))
    other.pairing_token = app.pairing_token
    await other.subscribe()
    reply = await other.publish("system", system_view())
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_OWNER


async def test_entities_write_only_on_change(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy")
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    first = hass.states.get("sensor.smart_occupancy_kamar_nordur_state")
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    second = hass.states.get("sensor.smart_occupancy_kamar_nordur_state")
    assert second.last_updated == first.last_updated
    assert isinstance(second.last_updated, datetime)
