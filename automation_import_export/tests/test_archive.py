"""The archive format: safe reading (malicious ZIPs), lossless YAML, and every verification check."""

from __future__ import annotations

import io
import json
import zipfile

from conftest import FakeHome, wait_op
import pytest

from aie import archive as A
from aie import yamlio
from aie.canonical import differences, same


def rezip(files: dict[str, bytes], tweak=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            if tweak:
                tweak(name, info)
            zf.writestr(info, data)
    return buf.getvalue()


def unzip(data: bytes) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    return {n: zf.read(n) for n in zf.namelist()}


async def exported(core, home: FakeHome, keys=None, mode=None) -> tuple[bytes, dict]:
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"keys": keys, "mode": mode} if mode else {"keys": keys}, "user-admin"))
    assert op["status"] == "completed", op["log"]
    return core.archives.read(op["archive_id"]), op


# ------------------------------------------------------------------ SafeZip
@pytest.mark.parametrize(("name", "code"), [
    ("../evil.yaml", "unsafe_path"), ("/etc/passwd", "unsafe_path"), ("a\\b.yaml", "unsafe_path"),
    ("a/../b.yaml", "unsafe_path"), ("a//b.yaml", "unsafe_path"), ("C:/x.yaml", "unsafe_path"), ("./x", "unsafe_path"),
])
def test_unsafe_names_are_refused(name, code):
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(rezip({"manifest.json": b"{}", name: b"x"}))
    assert err.value.code == code


def test_links_encryption_duplicates_and_sizes_are_refused(monkeypatch):
    def link(name, info):
        if name == "link":
            info.external_attr = 0o120777 << 16
    with pytest.raises(A.ArchiveError, match="link"):
        A.SafeZip(rezip({"manifest.json": b"{}", "link": b"/etc/passwd"}, link))
    data = bytearray(rezip({"manifest.json": b"{}"}))
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        data[data.find(sig) + off] |= 1
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(bytes(data))
    assert err.value.code == "encrypted"
    monkeypatch.setattr(A, "MAX_MEMBER_BYTES", 1000)
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(rezip({"manifest.json": b"{}", "big": b"0" * 2000}))
    assert err.value.code == "too_large"
    monkeypatch.setattr(A, "MAX_MEMBER_BYTES", 16 * 1024 * 1024)
    with pytest.raises(A.ArchiveError) as err:  # 4 MB of zeros compress ~1000:1
        A.SafeZip(rezip({"manifest.json": b"{}", "bomb": b"\0" * (4 * 1024 * 1024)}))
    assert err.value.code == "zip_bomb"
    monkeypatch.setattr(A, "MAX_MEMBERS", 3)
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(rezip({f"f{i}": b"x" for i in range(5)}))
    assert err.value.code == "too_many_files"
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(b"not a zip")
    assert err.value.code == "not_zip"
    monkeypatch.setattr(A, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(A.ArchiveError) as err:
        A.SafeZip(rezip({"a": b"x"}))
    assert err.value.code == "too_large"


def test_a_member_lying_about_its_size_is_damaged():
    data = bytearray(rezip({"manifest.json": b'{"format": "x"}'}))
    central = data.find(b"PK\x01\x02")
    data[central + 24:central + 28] = (5).to_bytes(4, "little")  # uncompressed size in the central directory
    z = A.SafeZip(bytes(data))
    with pytest.raises(A.ArchiveError):
        z.read("manifest.json")


def test_wrong_format_and_versions_are_refused():
    with pytest.raises(A.ArchiveError) as err:
        A.Archive(rezip({"automations.yaml": b"[]"}))
    assert err.value.code == "no_manifest"
    with pytest.raises(A.ArchiveError) as err:
        A.Archive(rezip({"manifest.json": b'{"format": "other"}'}))
    assert err.value.code == "wrong_format"
    for version in (99, 0, "1", None):
        with pytest.raises(A.ArchiveError) as err:
            A.Archive(rezip({"manifest.json": json.dumps({"format": A.ARCHIVE_FORMAT, "format_version": version}).encode()}))
        assert err.value.code == "unknown_version"


# ------------------------------------------------------------------ YAML
@pytest.mark.parametrize("value", [
    {"a": "on", "b": "yes", "c": "07:00", "d": "2026-11-22", "e": "1e3", "f": "null", "g": "~", "h": "0x1F", "i": ""},
    {"multi": "line one\nline two\n  indented", "trail": "spaces at the end   ", "lead": "  lead", "nl_end": "x\n"},
    {"unicode": "Kvøld ljós 🌙 – \"quoted\" 'single' #hash: colon", "tab": "a\tb", "cr": "a\r\nb"},
    {"nums": [0, -1, 1.5, 1e-7, 10**20, True, False, None], "nested": [{"x": [[], {}]}]},
    {"template": "{{ states('sensor.x') | float(0) > 2 }}", "star": "*", "amp": "&a", "bang": "!secret x"},
])
def test_yaml_dump_round_trips_exactly(value):
    assert same(yamlio.load(yamlio.dump(value)), value)


def test_same_distinguishes_types_and_differences_explains():
    assert not same({"a": 1}, {"a": 1.0}) and not same({"a": True}, {"a": 1}) and not same([1, 2], [2, 1])
    assert same({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert differences({"a": 1, "b": [1]}, {"a": 2, "c": 3}) == ["/a: 1 → 2", "/b: removed", "/c: added"]


# ------------------------------------------------------------------ build + verify
async def test_a_fresh_archive_verifies_and_contains_everything(core, home: FakeHome):
    data, op = await exported(core, home, ["1700000000001", "1700000000004"])
    report = op["verification"]
    assert report["ok"] and report["status"] == "verified", [c for c in report["checks"] if not c["ok"]]
    files = unzip(data)
    m = json.loads(files["manifest.json"])
    assert set(m["files"]) == set(files) - {"manifest.json"}
    assert m["source_installation"]["app_instance_id"] == core.identity.instance_id
    assert m["counts"]["blueprint_files"] == 1 and m["complete"]
    assert "automations.yaml" in files and yamlio.load(files["automations.yaml"].decode())[0]["id"] == "1700000000001"


async def test_verification_detects_tampering(core, home: FakeHome):
    data, _ = await exported(core, home, ["1700000000001", "1700000000002"])
    files = unzip(data)
    yaml_name = next(n for n in files if n.startswith("automations/0001"))

    def check(mutated: dict[str, bytes], check_id: str, **kw) -> dict:
        report = A.verify(rezip(mutated), **kw)
        failed = {c["id"] for c in report["checks"] if not c["ok"]}
        assert check_id in failed, report["checks"]
        assert not report["ok"]
        return report

    # a file changed without updating the manifest
    check({**files, yaml_name: files[yaml_name].replace(b"Kitchen", b"Kitchem")}, "checksums")
    # a YAML file that differs from the configuration (manifest updated consistently)
    tampered = dict(files)
    tampered[yaml_name] = files[yaml_name].replace(b"mode: single", b"mode: restart")
    m = json.loads(files["manifest.json"])
    m["files"][yaml_name] = {"sha256": A.sha256_bytes(tampered[yaml_name]), "size": len(tampered[yaml_name])}
    tampered["manifest.json"] = json.dumps(m).encode()
    report = check(tampered, "yaml")
    assert not report["automations"]["1700000000001"]["ok"]
    # a missing file
    check({k: v for k, v in files.items() if k != yaml_name}, "readable")
    # an extra file
    check({**files, "extra.txt": b"x"}, "readable")
    # a selection that is not complete
    check(files, "selection", expected_keys=["1700000000001", "1700000000002", "1700000000003"])
    # a protected automation offered for deletion
    data2, _ = await exported(core, home, ["automation_builder_dev_1"])
    report = A.verify(data2, deletion_keys=["automation_builder_dev_1"])
    assert "automation_builder_dev_1" in report["blocked"] and not report["deletable"]


async def test_verification_needs_metadata_definitions(core, home: FakeHome):
    data, _ = await exported(core, home, ["1700000000001"])
    files = unzip(data)
    labels = json.loads(files["data/labels.json"])
    del labels["night"]
    files["data/labels.json"] = json.dumps(labels).encode()
    m = json.loads(files["manifest.json"])
    m["files"]["data/labels.json"] = {"sha256": A.sha256_bytes(files["data/labels.json"]), "size": len(files["data/labels.json"])}
    files["manifest.json"] = json.dumps(m).encode()
    report = A.verify(rezip(files), deletion_keys=["1700000000001"])
    assert not next(c for c in report["checks"] if c["id"] == "metadata")["ok"]
    assert report["blocked"]["1700000000001"] and not report["deletable"]


async def test_live_comparison_detects_changes(core, home: FakeHome):
    data, _ = await exported(core, home, ["1700000000001", "1700000000002", "1700000000003"])
    home.file[0]["description"] = "changed"                                   # configuration
    home.registry["automation.hall_alarm"]["labels"] = ["security", "night"]   # labels
    home.states["automation.no_metadata"]["state"] = "off"                     # turned off
    live = await core.snapshot()
    report = A.verify(data, live=live, deletion_keys=["1700000000001", "1700000000002", "1700000000003"])
    assert report["deletable"] == []
    assert set(report["blocked"]) == {"1700000000001", "1700000000002", "1700000000003"}
    lossless = next(c for c in report["checks"] if c["id"] == "lossless")
    assert not lossless["ok"] and len(lossless["details"]) == 3


async def test_stored_archive_change_is_detected(core, home: FakeHome):
    data, op = await exported(core, home, ["1700000000003"])
    report = A.verify(data + b"x", stored_sha256=A.sha256_bytes(data))
    assert report["checks"][0]["id"] == "stored" and not report["ok"]


async def test_backup_mode_includes_protected_and_external_read_only(core, home: FakeHome):
    data, op = await exported(core, home, mode="all")
    archive = A.Archive(data)
    by = {r["key"]: r for r in archive.records}
    assert by["automation_builder_dev_1"]["protected"] and by["automation_builder_dev_1"]["restorable"] == "full"
    assert by["package_rule_1"]["restorable"] == "configuration_only"
    assert by["package_rule_1"]["config_origin"] == "automation/config"
    assert by["entity:automation.no_id_rule"]["config"]["alias"] == "No id rule"
    assert op["verification"]["status"] == "verified"


def test_yaml_alias_bombs_and_recursion_are_refused():
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{', '.join(['*' + chr(97 + i)] * 10)}]\n" for i in range(7))
    for loader in (yamlio.load, yamlio.load_tolerant):
        with pytest.raises(ValueError, match="too many"):
            loader(bomb)
        with pytest.raises(ValueError, match="too many"):
            loader("&a [*a]")
    assert yamlio.load("a: &x 1\nb: *x\n") == {"a": 1, "b": 1}  # ordinary anchors still work


async def test_the_download_name_is_safe_for_any_manifest(core, home: FakeHome):
    data, op = await exported(core, home, ["1700000000003"])
    files = unzip(data)
    m = json.loads(files["manifest.json"])
    m["created_at"] = '2026-11-22T10:00:00"\r\nX-Evil: 1'
    m["counts"]["automations"] = "1; rm"
    files["manifest.json"] = json.dumps(m).encode()
    archive_id = "aie-20260101T000000Z-abcdef"
    core.archives.add(archive_id, rezip(files), {"created_at": "x", "sha256": "x"})
    _, name = core.archive_file(archive_id)
    assert name == "automations-20261122T100000-x.zip"
