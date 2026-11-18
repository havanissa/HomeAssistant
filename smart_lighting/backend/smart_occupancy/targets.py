"""Unified lighting targets (0.6.0): ``light.*`` entities and relay outputs (``switch.*``).

A lighting target is anything automatic lighting may command:

* a ``light`` entity: on/off and, where its state says it supports them,
  brightness, colour temperature, colour, effects and transitions;
* a **relay output**: a ``switch`` entity the user explicitly designated as a
  lighting output of a room (``RoomLighting.relays``). It supports on/off only
  and is commanded with ``switch.turn_on`` / ``switch.turn_off``. No light
  attribute (brightness, colour, transition) is ever sent to a switch.

Switch entities that were not designated are never treated as lights: a
wall switch's button entities, physical controls and switch inputs stay what
they are (``switches.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant

from .lighting import Capabilities, light_capabilities

LIGHT = "light"
RELAY = "relay"
RELAY_DOMAIN = "switch"
LIGHTING_DOMAINS = frozenset({"light", RELAY_DOMAIN})
# A relay knows that it can be switched; nothing else.
RELAY_CAPABILITIES = Capabilities(known=True)


def domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def is_relay(entity_id: str) -> bool:
    return domain(entity_id) == RELAY_DOMAIN


def target_type(entity_id: str) -> str:
    return RELAY if is_relay(entity_id) else LIGHT


def capabilities(hass: HomeAssistant, entity_id: str) -> Capabilities:
    """What a target can do: a relay switches on and off only."""
    if is_relay(entity_id):
        return RELAY_CAPABILITIES
    return light_capabilities(hass, entity_id)


def service_data(entity_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """The service data a target accepts: nothing but the entity for a relay."""
    return {} if is_relay(entity_id) else dict(data)


def by_domain(entity_ids: Iterable[str]) -> dict[str, list[str]]:
    """Entities split by the domain whose services command them (light, switch)."""
    out: dict[str, list[str]] = {}
    for entity_id in entity_ids:
        out.setdefault(domain(entity_id), []).append(entity_id)
    return out
