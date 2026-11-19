"""Cover discovery, capabilities and live positions (0.6.1). Read only.

Every ``cover.*`` entity is a candidate. Its room comes from the entity's
area or its device's area. Capabilities come from ``supported_features``.
Proposed window groupings and layer kinds are suggestions from the entity
ids (``gardinur_<room>_vindeyga[_<side>]_<layer>``, where ``ljos``/``ljost``
is the sheer layer and ``myrk`` the blackout layer). The user confirms or
corrects every proposal; nothing is created and nothing moves on discovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from ..const import DOMAIN
from ..occupancy import friendly_name
from .config import CurtainConfig

MOVING_STATES = frozenset({"opening", "closing"})

# Name tokens -> layer kind (Faroese and English). Only a suggestion.
_KIND_TOKENS: dict[str, str] = {
    "ljos": "sheer", "ljost": "sheer", "ljós": "sheer", "ljóst": "sheer", "sheer": "sheer", "voile": "sheer",
    "semitransparent": "sheer", "transparent": "sheer", "tyll": "sheer",
    "myrk": "blackout", "myrkt": "blackout", "myrkur": "blackout", "blackout": "blackout", "dimout": "blackout",
    "shade": "shade", "blind": "shade", "blinds": "shade", "roller": "shade", "skuggi": "shade",
}
# Tokens that describe "a curtain on a window" rather than a particular window.
_FILLER = frozenset({"gardinur", "gardina", "gardinurnar", "curtain", "curtains", "cover", "covers", "vindeyga",
                     "vindeygad", "window", "windows", "glugga", "gluggi"})
_SIDES = {"nord": "Norður", "nordur": "Norður", "sudur": "Suður", "eystur": "Eystur", "vestur": "Vestur",
          "north": "North", "south": "South", "east": "East", "west": "West", "erva": "Erva", "nedra": "Neðra"}


def capabilities(state: State | None) -> dict[str, bool]:
    features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0) if state else 0
    return {
        "position": bool(features & CoverEntityFeature.SET_POSITION),
        "open": bool(features & CoverEntityFeature.OPEN),
        "close": bool(features & CoverEntityFeature.CLOSE),
        "stop": bool(features & CoverEntityFeature.STOP),
        "known": state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
    }


def available(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def current_position(state: State | None) -> int | None:
    """Reported position (0-100). Open/close-only covers: open = 100, closed = 0, anything else unknown."""
    if not available(state):
        return None
    raw = state.attributes.get("current_position")
    if raw is not None:
        try:
            return max(0, min(100, round(float(raw))))
        except (TypeError, ValueError):
            return None
    if state.state == "open":
        return 100
    if state.state == "closed":
        return 0
    return None


def members(hass: HomeAssistant, entity_id: str) -> set[str]:
    """The physical covers behind an entity: a cover group's members, otherwise the entity itself."""
    state = hass.states.get(entity_id)
    found = state.attributes.get(ATTR_ENTITY_ID) if state else None
    if isinstance(found, (list, tuple)) and found:
        return {m for m in found if isinstance(m, str) and m.startswith("cover.")} or {entity_id}
    return {entity_id}


def entity_area(hass: HomeAssistant, entity_id: str) -> str | None:
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        return device.area_id
    return None


def area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def _tokens(entity_id: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", entity_id.split(".", 1)[-1].casefold()) if t]


def guess_kind(entity_id: str, name: str | None = None) -> str:
    for token in [*_tokens(entity_id), *_tokens(f"x.{name or ''}")]:
        if token in _KIND_TOKENS:
            return _KIND_TOKENS[token]
    return "custom"


def window_key(entity_id: str) -> str:
    """Covers of one window share this key: the entity id without its layer word."""
    tokens = _tokens(entity_id)
    while len(tokens) > 1 and tokens[-1] in _KIND_TOKENS:
        tokens.pop()
    return "_".join(tokens)


def _proposal_name(key: str, area: str | None) -> str:
    """"Køkur Eystur" from gardinur_kokur_vindeyga_eystur in Køkur: filler and the room's own words are dropped."""
    area_tokens = {_fold(t) for t in _tokens(f"x.{area or ''}")}
    words = []
    for token in key.split("_"):
        word = _SIDES.get(token, token.capitalize())
        if token in _FILLER or _fold(token) in area_tokens or _fold(word) in area_tokens:
            continue
        words.append(word)
    base = area or "Window"
    return " ".join([base, *words]) if words else base


def _fold(text: str) -> str:
    table = str.maketrans({"ø": "o", "ð": "d", "á": "a", "í": "i", "ó": "o", "ú": "u", "ý": "y", "æ": "a", "é": "e"})
    return text.casefold().translate(table)


@dataclass(slots=True)
class Candidate:
    entity_id: str
    name: str
    area_id: str | None
    kind: str
    caps: dict[str, bool]
    assigned_to: str | None

    def as_dict(self, hass: HomeAssistant) -> dict[str, Any]:
        state = hass.states.get(self.entity_id)
        return {
            "entity_id": self.entity_id, "name": self.name, "area_id": self.area_id,
            "area_name": area_name(hass, self.area_id), "kind": self.kind, "caps": self.caps,
            "available": available(state), "position": current_position(state),
            "assigned_to": self.assigned_to, "members": sorted(members(hass, self.entity_id) - {self.entity_id}),
        }


def candidates(hass: HomeAssistant, config: CurtainConfig) -> list[Candidate]:
    """Every cover entity Home Assistant knows (registry and state machine), never our own."""
    ent_reg = er.async_get(hass)
    ids: set[str] = {e.entity_id for e in ent_reg.entities.values()
                     if e.domain == "cover" and e.platform != DOMAIN and e.disabled_by is None}
    ids |= {s.entity_id for s in hass.states.async_all("cover")
            if (entry := ent_reg.async_get(s.entity_id)) is None or entry.platform != DOMAIN}
    out = []
    for entity_id in ids:
        name = friendly_name(hass, entity_id) or entity_id
        out.append(Candidate(entity_id, name, entity_area(hass, entity_id), guess_kind(entity_id, name),
                             capabilities(hass.states.get(entity_id)), config.owner_of(entity_id)))
    out.sort(key=lambda c: (area_name(hass, c.area_id) or "~", c.name.casefold()))
    return out


def proposals(hass: HomeAssistant, config: CurtainConfig) -> list[dict[str, Any]]:
    """Suggested windows from the covers no window uses yet: same area + same key = one window."""
    taken: set[str] = set()
    for window in config.windows.values():
        for entity_id in window.entity_ids:
            taken |= members(hass, entity_id) | {entity_id}
    groups: dict[tuple[str | None, str], list[Candidate]] = {}
    for cand in candidates(hass, config):
        if cand.assigned_to or cand.entity_id in taken or members(hass, cand.entity_id) & taken:
            continue
        groups.setdefault((cand.area_id, window_key(cand.entity_id)), []).append(cand)
    out = []
    for (area_id, key), covers in groups.items():
        area = area_name(hass, area_id)
        out.append({
            "key": key, "name": _proposal_name(key, area), "area_id": area_id, "area_name": area,
            "covers": [{"entity_id": c.entity_id, "name": c.name, "kind": c.kind} for c in
                       sorted(covers, key=lambda c: (c.kind != "sheer", c.entity_id))],
        })
    out.sort(key=lambda p: (p["area_name"] or "~", p["name"].casefold()))
    return out
