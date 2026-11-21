"""The companion's HACS distribution (scripts/sod_companion_dist.py): layout, validation, determinism, drift, safety."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sod_companion_dist", REPO / "scripts" / "sod_companion_dist.py")
dist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dist)

INTEGRATION = "custom_components/smart_occupancy_detection"


@pytest.fixture(scope="module")
def tree():
    return dist.build_tree(None)


def test_layout_follows_hacs_rules(tree):
    assert {p.split("/")[1] for p in tree if p.startswith("custom_components/")} == {"smart_occupancy_detection"}
    assert {"README.md", "LICENSE", "hacs.json", "CHANGELOG.md", ".gitignore"} <= set(tree)
    manifest = json.loads(tree[f"{INTEGRATION}/manifest.json"])
    assert manifest["documentation"] == dist.REPO_URL and manifest["issue_tracker"] == f"{dist.REPO_URL}/issues"
    hacs = json.loads(tree["hacs.json"])
    assert hacs["name"] == "Smart Occupancy Detection" and hacs["homeassistant"]
    assert f"{INTEGRATION}/brand/icon.png" in tree
    assert not [p for p in tree if "__pycache__" in p or p.endswith(".pyc")]


def test_build_is_deterministic_and_matches_the_source(tree):
    assert dist.digest(tree) == dist.digest(dist.build_tree(None))
    source = REPO / INTEGRATION
    for path, content in tree.items():
        if path.startswith(INTEGRATION):
            assert (source / path[len(INTEGRATION) + 1:]).read_bytes() == content
    assert tree[f"{INTEGRATION}/protocol.py"] == (REPO / dist.APP_PROTOCOL).read_bytes()


def test_a_git_ref_builds_the_committed_version():
    head = dist.build_tree("HEAD")
    assert json.loads(head[f"{INTEGRATION}/manifest.json"])["domain"] == "smart_occupancy_detection"
    # The 0.1.0 release (Phase 0.1 merge commit) builds with the current templates.
    if subprocess.run(["git", "cat-file", "-e", "d53ebd7"], cwd=REPO).returncode == 0:
        old = dist.build_tree("d53ebd7", templates_ref="HEAD")
        assert json.loads(old[f"{INTEGRATION}/manifest.json"])["version"] == "0.1.0"


def test_check_detects_drift(tmp_path, tree):
    dist.write_tree(tree, tmp_path)
    assert dist.diff(tree, dist.read_dir(tmp_path)) == []
    (tmp_path / INTEGRATION / "hub.py").write_text("changed")
    (tmp_path / "extra.txt").write_text("x")
    (tmp_path / "README.md").unlink()
    assert dist.diff(tree, dist.read_dir(tmp_path)) == [
        "missing: README.md", "unexpected: extra.txt", f"differs: {INTEGRATION}/hub.py"]
    assert dist.main(["check", "--dist", str(tmp_path)]) == 1


@pytest.mark.parametrize(("change", "error"), [
    (lambda t: t.__setitem__(f"{INTEGRATION}/api.py", t[f"{INTEGRATION}/api.py"] + b"\n# ghp_" + b"a" * 30),
     "a GitHub token"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nsee /home/ragnar/notes"), "a local path"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nHA at 192.168.1.50"), "a private address"),
    (lambda t: t.__setitem__("custom_components/other/manifest.json", b"{}"), "exactly one integration"),
    (lambda t: t.__setitem__(f"{INTEGRATION}/__pycache__/x.pyc", b""), "must not be published"),
    (lambda t: t.__setitem__(f"{INTEGRATION}/const.py", b'VERSION: Final = "9.9.9"'), "const.VERSION differs"),
    (lambda t: t.pop("LICENSE"), "LICENSE is missing"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\npairing " + b"a1" * 16), "32\\+ hex id"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b'\ntoken = "abcdef123"'), "a credential"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nC:\\Users\\x"), "a local path"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\n/Users/ragnar/x"), "a local path"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\n/home/Ragnar/x"), "a local path"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nat 127.0.0.1"), "a private address"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nat fe80::1"), "a private address"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nopen myha.local"), "a local host name"),
    (lambda t: t.__setitem__("README.md", t["README.md"] + b"\nmail me@example.com"), "an e-mail address"),
    (lambda t: t.__setitem__("CHANGELOG.md", b"# Changelog\n"), "no section"),
])
def test_validation_refuses_unsafe_or_broken_packages(tree, change, error):
    broken = dict(tree)
    change(broken)
    with pytest.raises(dist.DistError, match=error):
        dist.validate(broken, (REPO / dist.APP_PROTOCOL).read_bytes())


def test_the_protocol_copy_must_match_the_app(tree):
    with pytest.raises(dist.DistError, match="protocol.py differs"):
        dist.validate(tree, b"something else")


def test_smart_comfort_stays_the_hacs_integration_of_the_main_repository():
    """HACS installs the first directory of custom_components/ in the repository tree (hacs utils/filters.py
    get_first_directory_in_directory): it must stay Smart Comfort's integration."""
    listing = subprocess.run(["git", "ls-tree", "HEAD", "custom_components/"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout.split("\n")
    directories = [line.split("\t")[1] for line in listing if "\ttree\t" in line or " tree " in line]
    assert directories[0] == "custom_components/smart_occupancy"
    assert json.loads((REPO / "hacs.json").read_text())["name"] == "Smart Occupancy & Lighting"


def test_png_text_chunks_are_scanned(tree):
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\0" * 13)
           + chunk(b"tEXt", b"Comment\0/home/ragnar/icons/x.svg") + chunk(b"IEND", b""))
    broken = {**tree, f"{INTEGRATION}/brand/icon.png": png}
    with pytest.raises(dist.DistError, match="a local path"):
        dist.validate(broken, (REPO / dist.APP_PROTOCOL).read_bytes())


@pytest.mark.parametrize("path", ["translations/x.py", "brand/a.py", "translations/brand/x.json", "sub/x.py",
                                  "brand/icon.svg", "secrets.yaml"])
def test_allowlist_is_exact(path):
    assert not dist.ALLOWED.match(path)


def test_publish_refuses_uncommitted_templates():
    with pytest.raises(dist.DistError, match="committed"):
        dist.publish("0.2.0", "HEAD", dry_run=True, allow_unmerged=True, templates_ref=None)
