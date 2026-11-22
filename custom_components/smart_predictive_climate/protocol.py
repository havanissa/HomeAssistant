"""Publishing protocol of Smart Adaptive & Predictive Climate: shared by the App and its companion integration.

The App observes, learns and predicts room temperatures; the companion
integration (``custom_components/smart_predictive_climate``) only shows the
App's results as native Home Assistant entities. This module is everything both
sides must agree on. It imports nothing from Home Assistant and nothing from
the App, so the two copies are byte-identical (``tests/test_package.py`` checks
it).

There is one control domain: reporting. Nothing in this protocol can command a
device. The companion accepts a result only from the one App session that holds
the current lease:

* **Pairing** - an administrator pairs the App from the App's interface. The
  companion keeps only a digest of the pairing token; a session that presents
  the token is *trusted*. Pairing again rotates the token and fences every
  older session.
* **Session** - one websocket subscription per App connection. It ends with the
  connection; nothing of a closed session is accepted afterwards.
* **Lease** - a trusted session claims publishing: a new ``epoch`` (it only
  ever increases and is persisted, so a result of an earlier epoch, also from
  before a restart, is always refused), a random lease token and sequence 0.
  Every accepted publish or heartbeat renews the lease for
  :data:`LEASE_SECONDS`; the App sends one at least every
  :data:`HEARTBEAT_SECONDS`.
* **Sequence** - every publish and heartbeat carries a strictly increasing
  ``seq``: duplicates and out-of-order messages are refused.
* **Snapshot** - a publish carries the complete, validated description of every
  room and of every entity the room has (never a delta), so the newest accepted
  message is always the whole truth::

      {"rooms": {room_id: {"name": str, "entities": {key: entity}}}, "deleted": [room_id, ...]}
      entity = {"platform": "sensor" | "binary_sensor", "name": str, "value": number | str | bool | None,
                "device_class": str | None, "unit": str | None, "state_class": str | None,
                "icon": str | None, "attributes": {...}, "category": None | "diagnostic"}

  The entities are generic: the App describes them (platform, device class,
  unit, name) and the companion shows them. ``value`` ``None`` means "unknown".
  A key that disappears from a room loses its entity. Rooms that are missing
  from a snapshot keep their entities, unavailable; only rooms listed in
  ``deleted`` (deleted by an administrator in the App) lose them.

Freshness: the entities are available only while the lease is valid *and* the
snapshot was accepted in the current epoch. When the App is away (lease
expired, session closed, Home Assistant restarted) every room entity is
unavailable: a stale forecast must never look like a recommendation.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re
from typing import Any

PROTOCOL_VERSION = 1
#: The companion integration's domain (also the websocket command prefix).
DOMAIN = "smart_predictive_climate"

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
MAX_ROOMS = 100
MAX_ENTITIES_PER_ROOM = 30
MAX_NAME = 80
MAX_VALUE = 255
MAX_UNIT = 20
MAX_ICON = 60
MAX_ATTRIBUTES = 30
MAX_ATTRIBUTE_BYTES = 4000
MAX_LIST = 50
MAX_SESSIONS = 4

ROOM_ID = re.compile(r"^[a-z0-9_]{1,100}$")
ENTITY_KEY = re.compile(r"^[a-z0-9_]{1,40}$")

PLATFORMS = ("sensor", "binary_sensor")
#: The device classes each platform may use (None = none).
DEVICE_CLASSES: dict[str, tuple[str | None, ...]] = {
    "sensor": (None, "temperature", "duration", "timestamp", "enum"),
    "binary_sensor": (None, "heat", "problem", "running", "connectivity"),
}
STATE_CLASSES = (None, "measurement")
CATEGORIES = (None, "diagnostic")
#: The units Home Assistant accepts for the numeric device classes.
UNITS: dict[str, tuple[str, ...]] = {
    "temperature": ("°C", "°F", "K"),
    "duration": ("s", "min", "h", "d"),
}
#: Fields of one published entity: name -> required. The optional ones default to None ({} for attributes).
ENTITY_FIELDS = {
    "platform": True, "name": True, "value": True, "device_class": False, "unit": False, "state_class": False,
    "icon": False, "attributes": False, "category": False,
}
ROOM_FIELDS = ("name", "entities")
SNAPSHOT_FIELDS = ("rooms", "deleted")
#: State attributes Home Assistant sets itself: an entity's ``attributes`` may not use these names.
RESERVED_ATTRIBUTES = frozenset({
    "friendly_name", "device_class", "unit_of_measurement", "state_class", "icon", "entity_picture",
    "supported_features", "restored", "assumed_state", "attribution",
})

REJECT_INVALID = "invalid"
REJECT_VERSION = "version_mismatch"
REJECT_NOT_PAIRED = "not_paired"
REJECT_ALREADY_PAIRED = "already_paired"
REJECT_UNKNOWN_SESSION = "unknown_session"
REJECT_NOT_OWNER = "not_owner"
REJECT_STALE_EPOCH = "stale_epoch"
REJECT_LEASE = "lease_invalid"
REJECT_SEQUENCE = "sequence"
REJECT_BUSY = "busy"
#: Codes that tell the App it no longer holds the lease (claim again before publishing).
LOST_CODES = frozenset({REJECT_NOT_OWNER, REJECT_STALE_EPOCH, REJECT_LEASE, REJECT_UNKNOWN_SESSION})

_ID = re.compile(r"^[0-9a-f]{32}$")
_VERSION = re.compile(r"^[0-9A-Za-z.\-+]{1,32}$")
_ICON = re.compile(r"^mdi:[a-z0-9\-]{1,56}$")
_ATTRIBUTE = re.compile(r"^[a-z0-9_]{1,40}$")


class ProtocolError(ValueError):
    """A message is refused; ``code`` is one of the ``REJECT_*`` codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _refuse(message: str) -> ProtocolError:
    return ProtocolError(REJECT_INVALID, message)


def valid_id(value: Any) -> bool:
    """Instance ids, pairing tokens and lease tokens are 32 lower-case hex characters."""
    return isinstance(value, str) and bool(_ID.match(value))


def valid_room_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ROOM_ID.match(value))


def valid_version(value: Any) -> bool:
    return isinstance(value, str) and bool(_VERSION.match(value))


def token_hash(token: str) -> str:
    """Only a digest of a pairing or lease token is stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_text(value: Any, limit: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (0 if empty else 1) <= len(value) <= limit


def _is_time(value: Any) -> bool:
    """A timezone-aware ISO-8601 time."""
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, bool) or _is_number(value) or _is_text(value, MAX_VALUE, empty=True)


def _attributes(raw: Any, where: str) -> dict[str, Any]:
    """Extra state attributes: JSON-safe scalars or short lists of scalars; bounded in number and size."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > MAX_ATTRIBUTES:
        raise _refuse(f"{where}: attributes must be an object with at most {MAX_ATTRIBUTES} keys")
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _ATTRIBUTE.match(name) or name in RESERVED_ATTRIBUTES:
            raise _refuse(f"{where}: invalid attribute name {name!r}")
        if isinstance(value, list):
            if len(value) > MAX_LIST or not all(_is_scalar(item) for item in value):
                raise _refuse(f"{where}: attribute {name} must be a list of at most {MAX_LIST} simple values")
            out[name] = list(value)
        elif _is_scalar(value):
            out[name] = value
        else:
            raise _refuse(f"{where}: attribute {name} must be a simple value or a list of simple values")
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_ATTRIBUTE_BYTES:
        raise _refuse(f"{where}: the attributes are larger than {MAX_ATTRIBUTE_BYTES} bytes")
    return out


def check_entity(key: Any, entity: Any, where: str = "") -> dict[str, Any]:
    """One published entity in its canonical form (every field present); raises ``ProtocolError``."""
    if not isinstance(key, str) or not ENTITY_KEY.match(key):
        raise _refuse(f"{where}invalid entity key {key!r}")
    where = f"{where}{key}"
    if not isinstance(entity, dict):
        raise _refuse(f"{where}: the entity must be an object")
    unknown = set(entity) - set(ENTITY_FIELDS)
    if unknown:
        raise _refuse(f"{where}: unknown fields {sorted(unknown, key=str)}")
    missing = [k for k, required in ENTITY_FIELDS.items() if required and k not in entity]
    if missing:
        raise _refuse(f"{where}: missing fields {missing}")
    platform = entity["platform"]
    if platform not in PLATFORMS:
        raise _refuse(f"{where}: platform must be one of {', '.join(PLATFORMS)}")
    name = entity["name"]
    if not _is_text(name, MAX_NAME) or not name.strip():
        raise _refuse(f"{where}: invalid name")
    device_class = entity.get("device_class")
    if not (device_class is None or isinstance(device_class, str)) or device_class not in DEVICE_CLASSES[platform]:
        raise _refuse(f"{where}: device class {device_class!r} is not allowed for a {platform}")
    state_class = entity.get("state_class")
    if not (state_class is None or isinstance(state_class, str)) or state_class not in STATE_CLASSES:
        raise _refuse(f"{where}: state class {state_class!r} is not allowed")
    unit = entity.get("unit")
    if unit is not None and not _is_text(unit, MAX_UNIT):
        raise _refuse(f"{where}: invalid unit")
    icon = entity.get("icon")
    if icon is not None and not (isinstance(icon, str) and len(icon) <= MAX_ICON and _ICON.match(icon)):
        raise _refuse(f"{where}: invalid icon")
    category = entity.get("category")
    if not (category is None or isinstance(category, str)) or category not in CATEGORIES:
        raise _refuse(f"{where}: category must be null or diagnostic")
    attributes = _attributes(entity.get("attributes"), where)
    value = entity["value"]
    if platform == "binary_sensor":
        if value is not None and not isinstance(value, bool):
            raise _refuse(f"{where}: the value of a binary sensor must be true, false or null")
        if state_class is not None or unit is not None:
            raise _refuse(f"{where}: a binary sensor has no state class and no unit")
    elif device_class == "timestamp":
        if value is not None and not _is_time(value):
            raise _refuse(f"{where}: the value must be a time with a time zone, or null")
        if state_class is not None or unit is not None:
            raise _refuse(f"{where}: a timestamp has no state class and no unit")
    elif device_class == "enum":
        options = attributes.get("options")
        if not isinstance(options, list) or not options or len(set(map(str, options))) != len(options) \
                or not all(_is_text(option, MAX_VALUE) for option in options):
            raise _refuse(f"{where}: an enum needs the attribute options (a list of different texts)")
        if value is not None and value not in options:
            raise _refuse(f"{where}: the value is not one of the options")
        if state_class is not None or unit is not None:
            raise _refuse(f"{where}: an enum has no state class and no unit")
    elif device_class in UNITS:
        if value is not None and not _is_number(value):
            raise _refuse(f"{where}: the value must be a finite number or null")
        if unit not in UNITS[device_class]:
            raise _refuse(f"{where}: the unit of a {device_class} must be one of {', '.join(UNITS[device_class])}")
    elif unit is not None or state_class is not None:
        if value is not None and not _is_number(value):
            raise _refuse(f"{where}: a sensor with a unit or a state class needs a finite number or null")
    elif value is not None and not _is_number(value) and not _is_text(value, MAX_VALUE, empty=True):
        raise _refuse(f"{where}: the value must be a number, a text of at most {MAX_VALUE} characters or null")
    return {"platform": platform, "name": name, "value": value, "device_class": device_class, "unit": unit,
            "state_class": state_class, "icon": icon, "attributes": attributes, "category": category}


def check_room(room_id: Any, room: Any) -> dict[str, Any]:
    """One room with its entities, validated (raises ``ProtocolError``); unknown fields are refused."""
    if not valid_room_id(room_id):
        raise _refuse(f"invalid room id {room_id!r}")
    if not isinstance(room, dict):
        raise _refuse(f"{room_id}: the room must be an object")
    unknown = set(room) - set(ROOM_FIELDS)
    if unknown:
        raise _refuse(f"{room_id}: unknown fields {sorted(unknown, key=str)}")
    name = room.get("name")
    if not _is_text(name, MAX_NAME) or not name.strip():
        raise _refuse(f"{room_id}: invalid name")
    entities = room.get("entities")
    if not isinstance(entities, dict) or len(entities) > MAX_ENTITIES_PER_ROOM:
        raise _refuse(f"{room_id}: entities must be an object with at most {MAX_ENTITIES_PER_ROOM} entities")
    return {"name": name, "entities": {key: check_entity(key, entity, f"{room_id}.")
                                       for key, entity in entities.items()}}


def unique_id(room_id: str, key: str) -> str:
    """The identity of a room entity in Home Assistant (stable across renames of the room)."""
    return f"{room_id}_{key}"


def check_snapshot(payload: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """A complete snapshot ``{"rooms": {...}, "deleted": [...]}``, validated: (rooms, deleted).

    ``deleted`` lists rooms an administrator deleted in the App: only these lose their entities. A room that is
    merely missing keeps its entities (unavailable), so a mistake can never delete entities.
    """
    if not isinstance(payload, dict):
        raise _refuse("the snapshot must be an object")
    unknown = set(payload) - set(SNAPSHOT_FIELDS)
    if unknown:
        raise _refuse(f"unknown fields {sorted(unknown, key=str)}")
    raw = payload.get("rooms")
    if not isinstance(raw, dict):
        raise _refuse("rooms must be an object")
    if len(raw) > MAX_ROOMS:
        raise _refuse(f"at most {MAX_ROOMS} rooms")
    rooms = {room_id: check_room(room_id, room) for room_id, room in raw.items()}
    seen: dict[tuple[str, str], str] = {}
    for room_id, room in rooms.items():
        for key, entity in room["entities"].items():
            identity = (entity["platform"], unique_id(room_id, key))
            if identity in seen:
                raise _refuse(f"{room_id}.{key} and {seen[identity]} would be the same entity")
            seen[identity] = f"{room_id}.{key}"
    deleted = payload.get("deleted")
    if deleted is None:
        return rooms, []
    if not isinstance(deleted, list) or len(deleted) > MAX_ROOMS:
        raise _refuse("deleted must be a list of room ids")
    for room_id in deleted:
        if not valid_room_id(room_id) or room_id in rooms:
            raise _refuse(f"invalid deleted room {room_id!r}")
    return rooms, list(dict.fromkeys(deleted))


def check_fence(params: dict[str, Any]) -> tuple[int, str, int]:
    """The fencing fields of a publish or heartbeat: (epoch, lease token, seq)."""
    epoch, token, seq = params.get("epoch"), params.get("lease_token"), params.get("seq")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ProtocolError(REJECT_INVALID, "epoch must be a positive integer")
    if not valid_id(token):
        raise ProtocolError(REJECT_INVALID, "invalid lease token")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 < seq < 2**53:
        raise ProtocolError(REJECT_INVALID, "seq must be a positive integer")
    return epoch, token, seq
