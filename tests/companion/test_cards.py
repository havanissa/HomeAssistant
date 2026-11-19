"""The compact dashboard cards (Phase A6): served by the companion, read-only data, actions only through the relay."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from custom_components.smart_occupancy import protocol as P
from custom_components.smart_occupancy.card import CARD_DIR
from custom_components.smart_occupancy.const import CARD_FILENAMES, COMPACT_CARDS_FILENAME, ROOM_CARD_FILENAME

CARDS = (CARD_DIR / COMPACT_CARDS_FILENAME).read_text(encoding="utf-8")


def test_the_card_files_are_shipped() -> None:
    assert set(CARD_FILENAMES) == {ROOM_CARD_FILENAME, COMPACT_CARDS_FILENAME}
    for name in CARD_FILENAMES:
        assert (CARD_DIR / name).is_file(), name
    assert not [p.name for p in CARD_DIR.glob("*.js") if p.name not in CARD_FILENAMES], "retired card files remain"


def test_the_retired_editor_cards_keep_their_names() -> None:
    """Dashboards configured for 0.9.0 keep working: the old types render the compact cards."""
    for name in ("smart-occupancy-mood-editor-card", "smart-occupancy-room-lighting-card", "smart-occupancy-curtains-card",
                 "smart-occupancy-house-card"):
        assert f'"{name}"' in CARDS, name
    assert CARDS.count("customElements.define(") == 6  # four cards + two editors


def test_cards_never_call_services_and_use_only_the_relay() -> None:
    assert "callService" not in CARDS and "callWS" not in CARDS and "fetch(" not in CARDS
    commands = set(re.findall(r'type: "(smart_occupancy/[a-z_/]+)"', CARDS))
    assert commands == {"smart_occupancy/companion/view", "smart_occupancy/companion/view/subscribe",
                        "smart_occupancy/companion/action"}
    # Every relayed action kind is one the companion accepts (and the App implements).
    kinds = set(re.findall(r'data-act="\$\{esc\(kind\)\}', CARDS))
    assert kinds, "actions go through the shared button helper"
    used = set(re.findall(r'button\([^;]*?"((?:room|mood|curtains)/[a-z]+)"', CARDS))
    assert used == set(P.ACTIONS), used ^ set(P.ACTIONS)
    assert "is_admin" in CARDS  # actions are offered to administrators only (the companion requires admin too)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_cards_parse() -> None:
    subprocess.run(["node", "--check", str(CARD_DIR / COMPACT_CARDS_FILENAME)], check=True)
    subprocess.run(["node", "--check", str(CARD_DIR / ROOM_CARD_FILENAME)], check=True)
