"""Shared occupancy engine of Smart Occupancy & Lighting (Phase A3.2).

One implementation, used by both runtimes:

* the integration (``custom_components/smart_occupancy/occupancy_core``,
  the original), and
* the Smart Lighting App (``smart_lighting/backend/occupancy_core``, a
  byte-identical copy made by ``scripts/sync_occupancy_core.py``; the App's
  build context cannot reach outside its folder).

The package has no dependencies outside the Python standard library and never
reads a clock or does I/O apart from reading its own files. Its
:func:`fingerprint` (SHA-256 of every source and fixture file) is exchanged on
the bridge: the integration hands occupancy over only to an App that runs the
very same engine.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path

#: Version of the shared engine. Bumped with every change of a rule or of a wire format.
CORE_VERSION = "1.0.0"

_ROOT = Path(__file__).resolve().parent


def source_files() -> list[Path]:
    """Every file that defines the engine's behaviour (sources and golden fixtures), in a stable order."""
    files = [p for p in _ROOT.rglob("*") if p.is_file() and p.suffix in (".py", ".json")
             and "__pycache__" not in p.parts]
    return sorted(files, key=lambda p: p.relative_to(_ROOT).as_posix())


@lru_cache(maxsize=1)
def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in source_files():
        digest.update(path.relative_to(_ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
