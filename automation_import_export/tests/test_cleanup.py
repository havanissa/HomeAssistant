"""Export & Delete: eligibility, protection, confirmation guards, change detection, interruption and recovery."""

from __future__ import annotations

import pytest
from conftest import FakeHome, wait_op

from aie.core import AppCore, OperationError

ORDINARY = ["1700000000001", "1700000000002", "1700000000003", "1700000000004", "1700000000005"]


async def start(core: AppCore, keys, **extra):
    await core.inventory_view()
    return await wait_op(core, await core.start_cleanup({"keys": keys, **extra}, "user-admin"))


async def confirm(core: AppCore, op, **override):
    body = {"operation_id": op["id"], "plan_hash": op["plan_hash"], "confirm_count": len(op["preview"]["delete"]),
            "archive_ack": True, **override}
    return await wait_op(core, await core.confirm_cleanup(body, "user-admin"))


async def test_protected_external_and_review_automations_are_excluded(core, home: FakeHome):
    everything = [i.key for i in (await core.snapshot()).items]
    op = await start(core, everything)
    excluded = op["selection"]["excluded"]
    assert "automation_builder_dev_1" in excluded and "Automation Builder" in excluded["automation_builder_dev_1"]
    assert "package_rule_1" in excluded and "entity:automation.no_id_rule" in excluded
    assert "1700000000007" in excluded and "ambiguous" in excluded["1700000000007"]
    assert sorted(d["key"] for d in op["preview"]["delete"]) == ORDINARY
    op = await confirm(core, op)
    assert op["status"] == "completed"
    assert [w for w in home.writes if w[0] == "delete"] == [("delete", k) for k in ORDINARY]
    assert home.ui_config("automation_builder_dev_1") is not None and "automation.package_rule" in home.states
    assert op["report"]["protected_changed"] == []
    assert op["report"]["external_remaining"] == 2


async def test_ambiguous_ownership_only_when_included_explicitly(core, home: FakeHome):
    op = await start(core, ["1700000000007"], include_review=["1700000000007"])
    assert [d["key"] for d in op["preview"]["delete"]] == ["1700000000007"]
    op = await confirm(core, op)
    assert home.ui_config("1700000000007") is None


async def test_nothing_is_deleted_without_a_valid_confirmation(core, home: FakeHome):
    op = await start(core, ["1700000000001", "1700000000003"])
    assert op["status"] == "awaiting_confirmation"
    for override, code in (({"confirm_count": 1}, "not_confirmed"), ({"archive_ack": False}, "not_confirmed"),
                           ({"plan_hash": "0" * 24}, "stale_plan")):
        with pytest.raises(OperationError) as err:
            await core.confirm_cleanup({"operation_id": op["id"], "plan_hash": op["plan_hash"], "confirm_count": 2,
                                        "archive_ack": True, **override}, "user-admin")
        assert err.value.code == code
    assert not [w for w in home.writes if w[0] == "delete"]
    await core.cancel({"operation_id": op["id"]}, "user-admin")
    with pytest.raises(OperationError):
        await confirm(core, op)
    assert home.ui_config("1700000000001") is not None


async def test_changes_after_the_export_are_never_deleted(core, home: FakeHome):
    op = await start(core, ["1700000000001", "1700000000002", "1700000000003"])
    home.file[0]["actions"].append({"delay": 1})                             # configuration changed
    home.registry["automation.hall_alarm"]["area_id"] = "kitchen"            # area changed
    op = await confirm(core, op)
    st = {k: v["status"] for k, v in op["items"].items()}
    assert st == {"1700000000001": "skipped_changed", "1700000000002": "skipped_changed", "1700000000003": "deleted"}
    assert op["status"] == "partial"
    assert home.ui_config("1700000000001") is not None and home.ui_config("1700000000002") is not None


async def test_a_change_between_export_and_verification_blocks_the_deletion(core, home: FakeHome):
    from aie.archive import verify
    op = await start(core, ["1700000000003"])
    assert op["status"] == "awaiting_confirmation"
    home.file[2]["alias"] = "changed during the export"
    report = verify(core.archives.read(op["archive_id"]), live=await core.snapshot(), deletion_keys=["1700000000003"])
    assert report["deletable"] == [] and "1700000000003" in report["blocked"]


async def test_builder_ids_are_refused_by_the_api_even_in_scope(core, home: FakeHome):
    from aie.ha import AutomationConfigApi, NotAllowed, WriteScope
    scope = WriteScope(deletions={"automation_builder_dev_1"}, configs={"automation_builder_dev_1"})
    api = AutomationConfigApi(None, "http://x/api", "t", scope)  # type: ignore[arg-type]
    with pytest.raises(NotAllowed):
        await api.delete("automation_builder_dev_1")
    with pytest.raises(NotAllowed):
        await api.put("AUTOMATION_BUILDER_dev_1", {})
    with pytest.raises(NotAllowed):
        await api.delete("1700000000001")  # not in the scope of a confirmed deletion


async def test_interrupted_deletion_is_reconciled_and_can_be_continued(core, home: FakeHome):
    keys = ORDINARY[:4]
    op = await start(core, keys)
    # the App stopped after two deletions (one of them not yet journalled as done)
    for key in keys[:2]:
        with home.scoped(deletions={key}):
            await core.api.delete(key)
    op["status"] = "deleting"
    op["items"][keys[0]]["status"] = "deleted"
    op["items"][keys[1]]["status"] = "deleting"
    core.journal.save(op)
    restarted = AppCore(core.settings)
    restarted.identity.load()
    restarted.archives.load()
    assert [o["id"] for o in restarted.journal.load()] == [op["id"]]
    restarted.attach(home, core.api)
    op = await restarted.reconcile({"operation_id": op["id"]}, "user-admin")
    st = {k: v["status"] for k, v in op["items"].items()}
    assert st == {keys[0]: "deleted", keys[1]: "deleted", keys[2]: "pending", keys[3]: "pending"}
    assert op["status"] == "partial" and op["resumable"]
    op = await restarted.resume_cleanup({"operation_id": op["id"]}, "user-admin")
    assert op["status"] == "awaiting_confirmation" and [d["key"] for d in op["preview"]["delete"]] == keys[2:]
    op = await wait_op(restarted, await restarted.confirm_cleanup({
        "operation_id": op["id"], "plan_hash": op["plan_hash"], "confirm_count": 2, "archive_ack": True}, "user-admin"))
    assert op["status"] == "completed"
    assert all(home.ui_config(k) is None for k in keys)


async def test_an_export_interrupted_while_writing_leaves_no_archive(core, home: FakeHome):
    (core.archives.dir / "aie-20260101T000000Z-abcdef.zip.tmp-123456").write_bytes(b"partial")
    op = core.journal.create("export", "user-admin", status="exporting")
    restarted = AppCore(core.settings)
    restarted.archives.load()
    interrupted = restarted.journal.load()
    assert [o["id"] for o in interrupted] == [op["id"]]
    assert not list(restarted.archives.dir.glob("*.tmp-*"))
    restarted.attach(home, core.api)
    op = await restarted.reconcile({"operation_id": op["id"]}, "user-admin")
    assert op["status"] == "failed"
