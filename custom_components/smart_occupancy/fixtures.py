"""Fixture types (0.6.8, Phase 4.8): how a managed lighting target is classified.

Pure configuration, per room (``RoomLighting.fixtures``), edited in Settings →
Lighting → Fixture Types. A classification is **descriptive** - a name for
the fixture and what kind of lamp it is - with one deliberate effect:

* **Dumb Light — On/Off only** (``dumb``): the target is commanded with on/off
  only. Its capabilities are *narrowed* to on/off (no brightness, colour,
  colour temperature, effect or transition is ever sent), even if the entity
  reports more. A relay output (``switch.*``) and an on/off-only ``light.*``
  are on/off anyway; for them the classification is the user-facing name.

A classification never *adds* a capability: what a target can do always comes
from the entity and its target type (``targets.py``). Classifying an entity
never makes it a lighting target either - a switch becomes a lighting output
only when the user explicitly designates it (``RoomLighting.relays``), and
nothing here changes Home Assistant's device configuration.

The other kinds (dimmable, tunable white, colour) and a custom label are names
only. A Home Assistant label may be chosen instead of (or with) an
integration-owned custom label; it does not have to exist for the
classification to work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .lighting import Capabilities

MAX_FIXTURES = 64
NAME_MAX = 60


class FixtureKind(StrEnum):
    DUMB = "dumb"                  # Dumb Light — On/Off only (relay or on/off light)
    DIMMABLE = "dimmable"
    WHITE = "tunable_white"
    COLOR = "color"
    CUSTOM = "custom"              # a custom label (``label``)


KIND_NAMES = {
    FixtureKind.DUMB: "Dumb Light — On/Off only",
    FixtureKind.DIMMABLE: "Dimmable light",
    FixtureKind.WHITE: "Tunable white light",
    FixtureKind.COLOR: "Colour light",
    FixtureKind.CUSTOM: "Custom",
}

ONOFF = Capabilities(known=True)


def _text(value: Any, limit: int = NAME_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()[:limit]
    return value or None


@dataclass(frozen=True, slots=True)
class Fixture:
    """The classification of one managed lighting target."""

    entity_id: str
    kind: FixtureKind = FixtureKind.DUMB
    label: str | None = None        # custom classification text (integration-owned)
    ha_label: str | None = None     # an existing Home Assistant label id (descriptive)
    name: str | None = None         # display name in Smart Occupancy

    @classmethod
    def from_dict(cls, entity_id: str, data: Any) -> Fixture | None:
        if not isinstance(entity_id, str) or "." not in entity_id or not isinstance(data, dict):
            return None
        try:
            kind = FixtureKind(data.get("kind"))
        except ValueError:
            return None
        return cls(entity_id=entity_id, kind=kind, label=_text(data.get("label")),
                   ha_label=_text(data.get("ha_label"), 100), name=_text(data.get("name")))

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "label": self.label, "ha_label": self.ha_label, "name": self.name}

    @property
    def dumb(self) -> bool:
        return self.kind == FixtureKind.DUMB

    def describe(self) -> str:
        if self.kind == FixtureKind.CUSTOM:
            return self.label or "Custom"
        base = KIND_NAMES[self.kind]
        return f"{base} · {self.label}" if self.label else base


def fixtures_from(data: Any) -> dict[str, Fixture]:
    out: dict[str, Fixture] = {}
    for entity_id, item in (data.items() if isinstance(data, dict) else []):
        fixture = Fixture.from_dict(entity_id, item)
        if fixture is not None and len(out) < MAX_FIXTURES:
            out[entity_id] = fixture
    return out


def restrict(caps: Capabilities, fixture: Fixture | None) -> Capabilities:
    """The capabilities used for commands: a Dumb Light is on/off only. Never more than the entity has."""
    if fixture is None or not fixture.dumb:
        return caps
    return ONOFF


def onoff_only(caps: Capabilities) -> bool:
    """Brightness matrix values of this target are On / Off (no dimming)."""
    return not caps.brightness
