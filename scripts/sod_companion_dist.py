#!/usr/bin/env python3
"""Build, check and publish the Smart Occupancy Detection companion's HACS repository.

The source of truth is this repository (``custom_components/smart_occupancy_detection``, templates in
``smart_occupancy_detection/companion_dist``). The HACS repository (https://github.com/havanissa/smart-occupancy-detection)
is generated from an exact git ref, so it can never drift unnoticed:

    python scripts/sod_companion_dist.py build --ref main --out /tmp/dist     # the tree that would be published
    python scripts/sod_companion_dist.py check --ref v0.2.0-src --dist <checkout of the HACS repository>
    python scripts/sod_companion_dist.py publish --version 0.2.0 --ref origin/main [--dry-run]

Only allowlisted files are copied (the integration's *.py / *.json / translations / brand icons, the templates and
the license); everything is scanned for secrets and local paths; the manifest, hacs.json and the layout are
validated against HACS's rules. ``publish`` refuses a ref that is not on ``origin/main`` (the authoritative branch),
a version that differs from the manifest, or a tag that already exists with other content; it pushes one commit and
an annotated tag ``v<version>`` and creates the GitHub release, then re-checks the published tag against the build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "smart_occupancy_detection"
REPO = "havanissa/smart-occupancy-detection"
REPO_URL = f"https://github.com/{REPO}"
INTEGRATION = f"custom_components/{DOMAIN}"
TEMPLATES = "smart_occupancy_detection/companion_dist"
APP_PROTOCOL = "smart_occupancy_detection/backend/sod/protocol.py"
#: Exact path shapes inside the integration directory (no wildcard crosses a "/").
ALLOWED = re.compile(r"^(?:[a-z_]+\.py|manifest\.json|strings\.json|translations/[a-z_-]+\.json|brand/[a-z@0-9_-]+\.png)$")
TEMPLATE_FILES = {"README.md": "README.md", "hacs.json": "hacs.json", "CHANGELOG.md": "CHANGELOG.md",
                  "gitignore": ".gitignore"}
MANIFEST_KEYS = ("domain", "name", "version", "documentation", "issue_tracker", "codeowners", "config_flow",
                 "iot_class", "requirements")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
FORBIDDEN = [
    (re.compile(rb"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), "a JSON web token"),
    (re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(rb"(?<![0-9a-fA-F])[0-9a-f]{32,}(?![0-9a-fA-F])"), "a 32+ hex id (token, pairing token or user id)"),
    (re.compile(rb"(?i)\b(password|passwd|secret|api_key|token|access_token|llat)\s*[=:]\s*['\"][^'\"]{4,}"),
     "a credential"),
    (re.compile(rb"(?i)/home/[^/\s]+/|/Users/[^/\s]+/|/root/|/tmp/claude|[A-Za-z]:\\\\?Users\\\\?"), "a local path"),
    (re.compile(rb"\b(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
                rb"\.\d{1,3}\.\d{1,3}\b|\b(?:fe80|fd[0-9a-f]{2}|fc[0-9a-f]{2})::?[0-9a-f:]*\b"), "a private address"),
    (re.compile(rb"(?i)\b[\w-]+\.(?:local|lan|home\.arpa)\b|\bui\.nabu\.casa\b|[\w-]+\.nabu\.casa"), "a local host name"),
    (re.compile(rb"[\w.+-]+@[\w-]+\.[\w.-]+"), "an e-mail address"),
]
#: Known safe text the scan would otherwise flag (codeowners and GitHub's noreply address).
ALLOW_TEXT = (b"@havanissa",)


class DistError(Exception):
    pass


def git(*args: str, cwd: Path = ROOT, binary: bool = False) -> str | bytes:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode()


def _files_at(ref: str | None, prefix: str) -> dict[str, bytes]:
    """Every file under ``prefix`` at ``ref`` (or in the working tree), relative to ``prefix``."""
    if ref is None:
        base = ROOT / prefix
        return {p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}
    names = git("ls-tree", "-r", "--name-only", ref, "--", prefix).split()
    return {n[len(prefix) + 1:]: git("show", f"{ref}:{n}", binary=True) for n in names}


def build_tree(ref: str | None = None, *, templates_ref: str | None = "same") -> dict[str, bytes]:
    """The distribution repository's files (path -> content), validated. ``ref`` None = the working tree."""
    tref = ref if templates_ref == "same" else templates_ref
    tree: dict[str, bytes] = {}
    for rel, content in sorted(_files_at(ref, INTEGRATION).items()):
        if ALLOWED.match(rel):
            tree[f"{INTEGRATION}/{rel}"] = content
    templates = _files_at(tref, TEMPLATES)
    for source, target in TEMPLATE_FILES.items():
        if source not in templates:
            raise DistError(f"template {source} is missing")
        tree[target] = templates[source]
    tree["LICENSE"] = (ROOT / "LICENSE").read_bytes() if tref is None else git("show", f"{tref}:LICENSE", binary=True)
    app_protocol = (ROOT / APP_PROTOCOL).read_bytes() if ref is None else git("show", f"{ref}:{APP_PROTOCOL}", binary=True)
    validate(tree, app_protocol)
    return tree


def validate(tree: dict[str, bytes], app_protocol: bytes | None = None) -> dict:
    """HACS's rules for an integration repository, and this project's own (no secrets, protocol copy, versions)."""
    dirs = {p.split("/")[1] for p in tree if p.startswith("custom_components/")}
    if dirs != {DOMAIN}:
        raise DistError(f"exactly one integration directory is allowed (custom_components/{DOMAIN}), found {dirs}")
    manifest = json.loads(tree[f"{INTEGRATION}/manifest.json"])
    missing = [k for k in MANIFEST_KEYS if k not in manifest]
    if missing:
        raise DistError(f"manifest.json misses {missing}")
    if manifest["domain"] != DOMAIN or not SEMVER.match(manifest["version"]):
        raise DistError("manifest domain or version is not valid")
    hacs = json.loads(tree["hacs.json"])
    if not hacs.get("name") or not hacs.get("homeassistant"):
        raise DistError("hacs.json needs name and homeassistant")
    for required in ("README.md", "LICENSE", f"{INTEGRATION}/__init__.py", f"{INTEGRATION}/config_flow.py",
                     f"{INTEGRATION}/brand/icon.png", f"{INTEGRATION}/translations/en.json"):
        if required not in tree:
            raise DistError(f"{required} is missing")
    const = tree[f"{INTEGRATION}/const.py"].decode()
    if f'VERSION: Final = "{manifest["version"]}"' not in const:
        raise DistError("const.VERSION differs from manifest.json")
    if app_protocol is not None and tree[f"{INTEGRATION}/protocol.py"] != app_protocol:
        raise DistError("protocol.py differs from the App's copy")
    for path, content in tree.items():
        if "__pycache__" in path or path.endswith((".pyc", ".env")) or "/." in "/" + path.replace(".gitignore", ""):
            raise DistError(f"{path} must not be published")
        text = png_text(content) if path.endswith(".png") else content
        for allowed in ALLOW_TEXT:
            text = text.replace(allowed, b"")
        for pattern, what in FORBIDDEN:
            if pattern.search(text):
                raise DistError(f"{path} contains {what}")
    if f"## {manifest['version']}" not in tree["CHANGELOG.md"].decode():
        raise DistError(f"CHANGELOG.md has no section for {manifest['version']}")
    return manifest


def png_text(content: bytes) -> bytes:
    """The text chunks of a PNG (tEXt, iTXt, zTXt; zTXt decompressed): metadata could carry names or paths."""
    import struct
    import zlib

    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DistError("not a PNG file")
    out, pos = [], 8
    while pos + 8 <= len(content):
        length, kind = struct.unpack(">I4s", content[pos:pos + 8])
        data = content[pos + 8:pos + 8 + length]
        if kind in (b"tEXt", b"iTXt"):
            out.append(data)
        elif kind == b"zTXt":
            key, _, rest = data.partition(b"\0")
            out.append(key + b" " + zlib.decompress(rest[1:]))
        pos += 12 + length
    return b"\n".join(out)


def write_tree(tree: dict[str, bytes], out: Path) -> None:
    for path, content in tree.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def read_dir(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(directory).parts}


def diff(expected: dict[str, bytes], actual: dict[str, bytes]) -> list[str]:
    out = [f"missing: {p}" for p in sorted(set(expected) - set(actual))]
    out += [f"unexpected: {p}" for p in sorted(set(actual) - set(expected))]
    out += [f"differs: {p}" for p in sorted(set(expected) & set(actual)) if expected[p] != actual[p]]
    return out


def digest(tree: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(path.encode() + b"\0" + tree[path] + b"\0")
    return h.hexdigest()


def release_notes(tree: dict[str, bytes], version: str, source: str) -> str:
    text = tree["CHANGELOG.md"].decode()
    section = text.split(f"## {version}", 1)[1].split("\n## ", 1)[0].strip()
    return f"{section}\n\nBuilt from havanissa/HomeAssistant@{source[:12]}."


def publish(version: str, ref: str, *, dry_run: bool, allow_unmerged: bool, templates_ref: str | None) -> int:
    if templates_ref is None:
        raise DistError("publish only from committed files: --templates-ref worktree is for build and check")
    git("fetch", "-q", "origin", "main")  # before resolving the ref, so origin/main is current
    source = git("rev-parse", ref).strip()
    if templates_ref != "same":
        templates_ref = git("rev-parse", templates_ref).strip()
    if not allow_unmerged:
        if subprocess.run(["git", "merge-base", "--is-ancestor", source, "origin/main"], cwd=ROOT).returncode != 0:
            raise DistError(f"{ref} is not on origin/main: publish from the authoritative branch")
    tree = build_tree(source, templates_ref=templates_ref)
    manifest = json.loads(tree[f"{INTEGRATION}/manifest.json"])
    if manifest["version"] != version:
        raise DistError(f"the manifest at {ref} is {manifest['version']}, not {version}")
    tag = f"v{version}"
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "dist"
        subprocess.run(["gh", "repo", "clone", REPO, str(clone), "--", "-q"], check=True)
        tags = git("tag", "--list", tag, cwd=clone).split()
        if tags:
            git("checkout", "-q", tag, cwd=clone)
            problems = diff(tree, read_dir(clone))
            if problems:
                raise DistError(f"{tag} already exists with other content: {problems[:5]}")
            print(f"{tag} is already published and identical to {ref}")
            return 0
        has_main = bool(git("ls-remote", "--heads", "origin", "main", cwd=clone).strip())
        if has_main:
            git("checkout", "-q", "main", cwd=clone)
        else:
            git("checkout", "-q", "-b", "main", cwd=clone)
        for item in clone.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        write_tree(tree, clone)
        git("add", "-A", cwd=clone)
        message = f"Release {tag}\n\nBuilt from havanissa/HomeAssistant@{source} by scripts/sod_companion_dist.py."
        git("-c", "user.name=havanissa", "-c", "user.email=havanissa@users.noreply.github.com",
            "commit", "-q", "--allow-empty", "-m", message, cwd=clone)
        git("-c", "user.name=havanissa", "-c", "user.email=havanissa@users.noreply.github.com",
            "tag", "-a", tag, "-m", f"Smart Occupancy Detection {version}", cwd=clone)
        if dry_run:
            print(f"dry run: would push main and {tag} ({len(tree)} files, digest {digest(tree)[:16]})")
            return 0
        git("push", "-q", "origin", "main", tag, cwd=clone)
        notes = release_notes(tree, version, source)
        subprocess.run(["gh", "release", "create", tag, "--repo", REPO, "--title", f"v{version}", "--notes", notes,
                        "--verify-tag"], check=True)
    # Re-check what GitHub now serves for the tag.
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "check"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", tag, f"{REPO_URL}.git", str(clone)], check=True)
        problems = diff(tree, read_dir(clone))
        if problems:
            raise DistError(f"the published {tag} differs from the build: {problems}")
    print(f"published {tag} from {source[:12]} ({len(tree)} files); verified")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--ref")
    b.add_argument("--out", required=True, type=Path)
    c = sub.add_parser("check")
    c.add_argument("--ref")
    c.add_argument("--dist", required=True, type=Path)
    p = sub.add_parser("publish")
    p.add_argument("--version", required=True)
    p.add_argument("--ref", default="origin/main")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-unmerged", action="store_true")
    p.add_argument("--templates-ref", default="same",
                   help="read README/hacs.json/CHANGELOG from another ref ('worktree' = the working tree); used to "
                        "publish an older integration with the current documentation")
    for parser_ in (b, c):
        parser_.add_argument("--templates-ref", default="same")
    args = parser.parse_args(argv)
    templates_ref = None if args.templates_ref == "worktree" else args.templates_ref
    try:
        if args.command == "build":
            tree = build_tree(args.ref, templates_ref=templates_ref)
            if args.out.exists() and any(args.out.iterdir()):
                raise DistError(f"{args.out} is not empty")
            write_tree(tree, args.out)
            print(f"built {len(tree)} files into {args.out} (digest {digest(tree)[:16]})")
            return 0
        if args.command == "check":
            problems = diff(build_tree(args.ref, templates_ref=templates_ref), read_dir(args.dist))
            for line in problems:
                print(line)
            print("in sync" if not problems else f"DRIFT: {len(problems)} difference(s)")
            return 1 if problems else 0
        return publish(args.version, args.ref, dry_run=args.dry_run, allow_unmerged=args.allow_unmerged,
                       templates_ref=templates_ref)
    except DistError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
