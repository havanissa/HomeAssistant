"""The retired 0.9.0 data: read-only export, reviewed retirement with a backup, restoration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smart_occupancy import protocol as P
from custom_components.smart_occupancy.const import DOMAIN, LEGACY_BACKUP_STORE_KEY, LEGACY_STORES

from .conftest import FakeApp, legacy_options, setup_companion


async def seed_legacy(hass: HomeAssistant) -> MockConfigEntry:
    """A 0.9.0 installation: entry 1.2 with its options, and its stores (with their own versions)."""
    await Store(hass, 1, f"{DOMAIN}.lighting", minor_version=9).async_save({"rooms": {"kamar_nordur": {"control_enabled": True}},
                                                                            "global_profiles": {}})
    await Store(hass, 1, f"{DOMAIN}.lighting_state").async_save({"rooms": {"kamar_nordur": {"overrides": {"light.kamar_lamp": 1}}}})
    await Store(hass, 1, f"{DOMAIN}.curtains", minor_version=4).async_save({"windows": {}, "presets": {}})
    await Store(hass, 1, f"{DOMAIN}.curtains_state").async_save({"manual": {"w1": {"hold": "until_empty"}}})
    await Store(hass, 1, f"{DOMAIN}.forecast_days").async_save({"days": {"2026-11-18": {"rooms": {}}}})
    await Store(hass, 1, f"{DOMAIN}.occupancy_state").async_save({"rooms": {"kamar_nordur": {"engine": {"state": "vacant"}}}})
    await Store(hass, 1, f"{DOMAIN}.lighting_ownership").async_save({"owner": "app", "epoch": 3})
    await Store(hass, 1, f"{DOMAIN}.engine_ownership").async_save({"owner": "integration", "epoch": 2})
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", data={}, options=legacy_options(),
                            version=1, minor_version=2, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    return entry


async def test_export_is_complete_and_read_only(hass: HomeAssistant, hass_ws_client) -> None:
    entry = await seed_legacy(hass)
    await setup_companion(hass, entry=entry)
    app = FakeApp(hass, await hass_ws_client(hass))
    hello = await app.subscribe(token=None)
    assert hello["legacy"] == {"present": True, "retired": None, "options_revision": P.revision(legacy_options()),
                               "rooms": 1, "entry_minor_version": 3}
    export = (await app.send(type="smart_occupancy/companion/legacy/export"))["result"]
    assert export["schema"] == 1 and export["entry"]["options"] == legacy_options()
    assert export["stores"][f"{DOMAIN}.lighting"] == {"version": 1, "minor_version": 9,
                                                       "data": {"rooms": {"kamar_nordur": {"control_enabled": True}},
                                                                "global_profiles": {}}}
    assert export["stores"][f"{DOMAIN}.curtains"]["minor_version"] == 4
    assert export["stores"][f"{DOMAIN}.forecast_settings"] is None
    assert export["legacy_owners"] == {"occupancy": "integration", "lighting": "app", "curtains": None, "forecast": None}
    # Exporting changed nothing.
    assert (await Store(hass, 1, f"{DOMAIN}.lighting", minor_version=9).async_load())["global_profiles"] == {}
    assert dict(entry.options) == legacy_options()
    status = (await app.send(type="smart_occupancy/companion/legacy/status"))["result"]
    assert status["present"] and status["rooms"] == 1 and status["backup"] is None
    assert f"{DOMAIN}.occupancy_state" in status["stores"]


async def test_retire_backs_up_then_removes_and_restore_brings_back(hass: HomeAssistant, hass_ws_client, hass_admin_user) -> None:
    entry = await seed_legacy(hass)
    await setup_companion(hass, entry=entry)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.subscribe(token=None)
    reply = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=False)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_INVALID
    reply = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True,
                           expected_options_revision="0" * 16)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_BUSY  # inspected data changed meanwhile
    reply = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True,
                           expected_options_revision=P.revision(legacy_options()), requested_by="admin")
    assert reply["success"], reply
    result = reply["result"]
    assert set(result["stores_removed"]) == {f"{DOMAIN}.lighting", f"{DOMAIN}.lighting_state", f"{DOMAIN}.curtains",
                                             f"{DOMAIN}.curtains_state", f"{DOMAIN}.forecast_days",
                                             f"{DOMAIN}.occupancy_state", f"{DOMAIN}.lighting_ownership",
                                             f"{DOMAIN}.engine_ownership"}
    assert entry.options["companion"]["options_revision"] == P.revision(legacy_options())
    assert "rooms" not in entry.options
    for key in LEGACY_STORES:
        assert await Store(hass, 1, key).async_load() is None, key
    backup = await Store(hass, 1, LEGACY_BACKUP_STORE_KEY).async_load()
    assert backup["by"] == hass_admin_user.id and backup["export"]["entry"]["options"] == legacy_options()
    assert backup["export"]["stores"][f"{DOMAIN}.curtains_state"]["data"]["manual"]["w1"]["hold"] == "until_empty"
    status = (await app.send(type="smart_occupancy/companion/legacy/status"))["result"]
    assert status["present"] is False and status["retired"]["by"] == hass_admin_user.id and status["backup"]["rooms"] == 1
    reply = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NO_LEGACY
    # Restore: the stores and the options come back, with their versions (a rollback to 0.9.0 can load them).
    reply = await app.send(type="smart_occupancy/companion/legacy/restore", confirm=True)
    assert reply["success"] and reply["result"]["options"] is True
    assert dict(entry.options) == legacy_options()
    assert (await Store(hass, 1, f"{DOMAIN}.lighting", minor_version=9).async_load())["rooms"] == {
        "kamar_nordur": {"control_enabled": True}}
    assert (await Store(hass, 1, f"{DOMAIN}.curtains", minor_version=4).async_load()) == {"windows": {}, "presets": {}}
    status = (await app.send(type="smart_occupancy/companion/legacy/status"))["result"]
    assert status["present"] is True and status["retired"] is None
    # The backup is never deleted by a restore.
    assert (await Store(hass, 1, LEGACY_BACKUP_STORE_KEY).async_load())["export"]["entry"]["options"] == legacy_options()


async def test_a_fresh_installation_has_nothing_to_retire(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.subscribe(token=None)
    reply = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NO_LEGACY
    reply = await app.send(type="smart_occupancy/companion/legacy/restore", confirm=True)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NO_LEGACY


async def test_the_backup_is_never_replaced_by_a_weaker_export(hass: HomeAssistant, hass_ws_client, areas) -> None:
    """The legacy backup store is the only copy of the retired data: a retirement that would drop part of it is refused."""
    entry = await seed_legacy(hass)
    await setup_companion(hass, entry=entry)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.subscribe(token=None)
    first = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True)
    assert first["success"], first
    restored = await app.send(type="smart_occupancy/companion/legacy/restore", confirm=True)
    assert restored["success"] and restored["result"]["options"] is True
    # Someone removes the options by hand (a half-finished restore): the stores are back, the options are not.
    entry = hass.config_entries.async_entries("smart_occupancy")[0]
    hass.config_entries.async_update_entry(entry, options={"companion": {"retired_at": "x"}})
    await hass.async_block_till_done()
    second = await app.send(type="smart_occupancy/companion/legacy/retire", confirm=True)
    assert not second["success"] and second["error"]["code"] == P.REJECT_BUSY, second
    status = (await app.send(type="smart_occupancy/companion/legacy/status"))["result"]
    assert status["backup"]["rooms"] == 1  # the backup still holds the options
