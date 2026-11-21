"""Install the companion into a (test) Home Assistant configuration the way HACS does.

HACS downloads the tagged repository content and replaces ``custom_components/<domain>`` with the repository's
``custom_components/<domain>`` (the first directory there). This does the same from the published GitHub tag, or
from a local build of the distribution (``scripts/sod_companion_dist.py build``):

    python hacs_install.py <config dir> --tag v0.1.0
    python hacs_install.py <config dir> --dir <built distribution>
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

REPO = "havanissa/smart-occupancy-detection"
DOMAIN = "smart_occupancy_detection"


def first_integration(root: Path) -> Path:
    directories = sorted(p for p in (root / "custom_components").iterdir() if p.is_dir())
    return directories[0]  # HACS: the first directory in custom_components/


def install(config: Path, source_root: Path) -> str:
    source = first_integration(source_root)
    target = config / "custom_components" / DOMAIN
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return json.loads((target / "manifest.json").read_text())["version"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--dir", type=Path)
    args = parser.parse_args()
    if args.tag:
        url = f"https://github.com/{REPO}/archive/refs/tags/{args.tag}.tar.gz"
        with urllib.request.urlopen(url, timeout=60) as response, tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(response.read()), mode="r:gz") as archive:
                archive.extractall(tmp, filter="data")
            root = next(Path(tmp).iterdir())
            print(f"installed {install(args.config, root)} from {REPO}@{args.tag}")
    else:
        print(f"installed {install(args.config, args.dir)} from {args.dir}")


if __name__ == "__main__":
    main()
