"""The companion link: pairing on request, claim, complete snapshots, heartbeats, fencing and re-claiming."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from sod import config as C, publisher as PUB
from sod.publisher import CompanionLink
from sod.runtime import OccupancyRuntime
from tests.conftest import KAMAR, room, settle

PIR, RADAR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(PUB, "PUBLISH_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RECLAIM_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(PUB.P, "HEARTBEAT_SECONDS", 0.05)


async def setup(tmp_path, ha, clock, can_publish=lambda: None):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save({"timing": {**C.empty_config()["timing"], "startup_hold": 0}, "rooms": {KAMAR: room()}},
               expected_revision=store.revision, reason="test")
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = OccupancyRuntime(ha, store, tmp_path)
    link = CompanionLink(ha, runtime.published_rooms, tmp_path, app_version="0.1.0", can_publish=can_publish)
    runtime.add_listener(lambda _a: link.schedule_publish())
    await runtime.start()
    await link.start()
    return runtime, link


async def wait_for(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_nothing_is_published_before_an_administrator_pairs(tmp_path, ha, clock):
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    assert ha.companion.publishes == 0 and ha.commands_sent[f"{PUB.P.DOMAIN}/claim"] == 0
    await link.pair(replace=False, user_id="admin")
    await wait_for(lambda: ha.companion.publishes == 1)
    assert ha.companion.views[KAMAR]["state"] == "vacant" and link.state == "publishing"
    token_file = tmp_path / "companion.json"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    # A change of the decision is published as a new complete snapshot.
    clock.advance(5)
    ha.set(PIR, "on", clock.now)
    await wait_for(lambda: ha.companion.views[KAMAR]["occupied"] is True)
    publishes = ha.companion.publishes
    # Nothing changed: no new snapshot, but heartbeats keep the lease.
    await asyncio.sleep(0.2)
    assert ha.companion.publishes == publishes and link.counters["heartbeats"] >= 1
    await link.stop()
    await runtime.stop()
    assert ha.companion.lease is None  # released at shutdown


async def test_a_lost_lease_is_claimed_again_with_a_new_epoch(tmp_path, ha, clock):
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, user_id="admin")
    await wait_for(lambda: link.state == "publishing")
    epoch = ha.companion.epoch
    ha.companion.expire_lease()
    clock.advance(5)
    ha.set(PIR, "on", clock.now)
    await wait_for(lambda: ha.companion.epoch == epoch + 1 and ha.companion.views[KAMAR]["occupied"] is True)
    assert link.counters["refused"] >= 1 and link.state == "publishing"
    await link.stop()
    await runtime.stop()


async def test_a_new_app_process_resumes_with_the_stored_pairing(tmp_path, ha, clock):
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, user_id="admin")
    await wait_for(lambda: link.state == "publishing")
    await link.stop()
    await runtime.stop()
    ha.disconnect()
    runtime2, link2 = await setup(tmp_path, ha, clock)
    assert link2.instance_id != link.instance_id
    ha.connect()
    await wait_for(lambda: link2.state == "publishing")  # no human needed after a restart
    await link2.stop()
    await runtime2.stop()


async def test_integration_missing_or_paired_elsewhere(tmp_path, ha, clock):
    ha.companion.installed = False
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_installed")
    ha.companion.installed = True
    ha.companion.loaded = False
    await wait_for(lambda: link.state == "not_set_up")
    ha.companion.loaded = True
    await wait_for(lambda: link.state == "not_paired")
    # Another App installation pairs first: this one must ask before replacing it.
    ha.companion.pairing_hash = "0" * 64
    ha.disconnect()
    ha.connect()
    await wait_for(lambda: link.state == "pair_mismatch")
    with pytest.raises(PUB.LinkError) as err:
        await link.pair(replace=False, user_id="admin")
    assert err.value.code == "already_paired"
    await link.pair(replace=True, user_id="admin")
    await wait_for(lambda: link.state == "publishing")
    await link.unpair(user_id="admin")
    assert link.state == "not_paired" and not (tmp_path / "companion.json").exists()
    await link.stop()
    await runtime.stop()


async def test_a_failed_self_test_blocks_publishing(tmp_path, ha, clock):
    runtime, link = await setup(tmp_path, ha, clock, can_publish=lambda: "self-test failed")
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, user_id="admin")
    await settle()
    assert link.state == "blocked" and ha.companion.publishes == 0
    await link.stop()
    await runtime.stop()
