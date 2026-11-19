"""The companion protocol: identical on both sides, and the fences it defines."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.smart_occupancy import protocol as P

ROOT = Path(__file__).resolve().parents[2]


def test_the_app_ships_the_same_protocol_module() -> None:
    source = ROOT / "custom_components" / "smart_occupancy" / "protocol.py"
    copy = ROOT / "smart_lighting" / "backend" / "smart_lighting" / "companion_protocol.py"
    assert source.read_bytes() == copy.read_bytes(), "run `python scripts/check_protocol.py --sync`"


def test_command_fences() -> None:
    targets = P.check_targets("lighting", {"lights": ["light.a", "light.b"], "relays": ["switch.r"],
                                           "actions": ["scene.movie", "select.mood"]})
    # Allowed: configured lights, relays and actions; only the published targets.
    P.check_command("lighting", "light", "turn_on", {"entity_id": "light.a", "brightness": 100}, targets=targets)
    P.check_command("lighting", "light", "turn_off", {"entity_id": ["light.a", "light.b"]}, targets=targets)
    P.check_command("lighting", "switch", "turn_on", {"entity_id": "switch.r"}, targets=targets)
    P.check_command("lighting", "scene", "turn_on", {"entity_id": "scene.movie"}, targets=targets)
    for bad in [("light", "turn_on", {"entity_id": "light.other"}), ("light", "turn_on", {"entity_id": "switch.r"}),
                ("switch", "turn_on", {"entity_id": "light.a"}), ("homeassistant", "restart", {}),
                ("light", "turn_on", {"entity_id": "light.a", "area_id": "kitchen"}),
                ("light", "turn_on", {}), ("cover", "open_cover", {"entity_id": "cover.x"}),
                ("light", "turn_on", {"entity_id": "light.a", "brightness": 999}),
                ("script", "turn_on", {"entity_id": "script.anything"})]:
        with pytest.raises(P.ProtocolError):
            P.check_command("lighting", *bad, targets=targets)
    covers = P.check_targets("curtains", {"covers": ["cover.c"]})
    P.check_command("curtains", "cover", "set_cover_position", {"entity_id": "cover.c", "position": 40}, targets=covers)
    P.check_command("curtains", "cover", "open_cover", {"entity_id": "cover.c"}, targets=covers)
    for bad in [("cover", "set_cover_position", {"entity_id": "cover.c", "position": 101}),
                ("cover", "set_cover_position", {"entity_id": ["cover.c", "cover.c"], "position": 10}),
                ("cover", "stop_cover", {"entity_id": "cover.c"}), ("light", "turn_on", {"entity_id": "cover.c"}),
                ("cover", "open_cover", {"entity_id": "cover.other"})]:
        with pytest.raises(P.ProtocolError):
            P.check_command("curtains", *bad, targets=covers)
    with pytest.raises(P.ProtocolError):
        P.check_command("occupancy", "light", "turn_on", {"entity_id": "light.a"}, targets={})
    with pytest.raises(P.ProtocolError):
        P.check_command("forecast", "light", "turn_on", {"entity_id": "light.a"}, targets={})
