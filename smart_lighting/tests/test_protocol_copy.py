"""Phase A6: the App's copy of the companion protocol is the companion's module, byte for byte."""

from __future__ import annotations

from pathlib import Path

from smart_lighting import companion_protocol as P

REPO = Path(__file__).resolve().parents[2]


def test_the_protocol_module_is_identical_on_both_sides() -> None:
    source = REPO / "custom_components" / "smart_occupancy" / "protocol.py"
    copy = Path(P.__file__)
    assert source.read_bytes() == copy.read_bytes(), "run `python scripts/check_protocol.py --sync`"
    assert P.PROTOCOL_VERSION == 1 and set(P.COMMAND_DOMAINS) == {"lighting", "curtains"}
