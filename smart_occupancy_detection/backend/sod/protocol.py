"""Publishing protocol of Smart Occupancy Detection: shared by the App and its companion integration.

The App decides room occupancy; the companion integration
(``custom_components/smart_occupancy_detection``) only shows the App's results
as native Home Assistant entities. This module is everything both sides must
agree on. It imports nothing from Home Assistant and nothing from the App, so
the two copies are byte-identical (``tests/test_package.py`` checks it).

There is one control domain: occupancy reporting. The companion accepts a
result only from the one App session that holds the current lease:

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
* **Snapshot** - a publish carries the complete, validated view of every
  enabled room (never a delta), so the newest accepted message is always the
  whole truth. Rooms that are missing from it keep their entities, unavailable;
  only rooms listed in ``deleted`` (deleted by an administrator in the App)
  lose them.

Freshness: the views are fresh only while the lease is valid *and* they were
accepted in the current epoch. The companion's entities keep the last accepted view while the App
is away (lease expired, session closed, Home Assistant restarted); they report
``fresh: false`` and the companion's connection sensor turns off. An absent App
never makes a room vacant.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from typing import Any

PROTOCOL_VERSION = 1
#: The companion integration's domain (also the websocket command prefix).
DOMAIN = "smart_occupancy_detection"

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
MAX_ROOMS = 200
MAX_NAME = 100
MAX_REASON = 255
MAX_SESSIONS = 4

#: The detailed occupancy states (``occupancy_core.types.OccupancyState``).
STATES = ("vacant", "possible_occupancy", "confirmed_occupancy", "possible_vacancy", "uncertain")

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
_AREA_ID = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")
_CODE = re.compile(r"^[a-z_]{1,64}$")
_VERSION = re.compile(r"^[0-9A-Za-z.\-+]{1,32}$")

#: Fields of one published room view: name -> required.
ROOM_FIELDS = {
    "name": True, "state": True, "occupied": True, "confidence": True, "reason": True, "reason_code": True,
    "last_activity": True, "state_since": True, "vacancy_at": True, "starting_up": True, "sensor_problems": True,
}


class ProtocolError(ValueError):
    """A message is refused; ``code`` is one of the ``REJECT_*`` codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def valid_id(value: Any) -> bool:
    """Instance ids, pairing tokens and lease tokens are 32 lower-case hex characters."""
    return isinstance(value, str) and bool(_ID.match(value))


def valid_area_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_AREA_ID.match(value))


def valid_version(value: Any) -> bool:
    return isinstance(value, str) and bool(_VERSION.match(value))


def token_hash(token: str) -> str:
    """Only a digest of a pairing or lease token is stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def _time(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ProtocolError(REJECT_INVALID, f"{key}: not a time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise ProtocolError(REJECT_INVALID, f"{key}: not a time") from err
    if parsed.tzinfo is None:
        raise ProtocolError(REJECT_INVALID, f"{key}: time without a time zone")
    return value


def check_room(area_id: Any, view: Any) -> dict[str, Any]:
    """One room's published view, validated (raises ``ProtocolError``); unknown fields are refused."""
    if not valid_area_id(area_id):
        raise ProtocolError(REJECT_INVALID, f"invalid room id {area_id!r}")
    if not isinstance(view, dict):
        raise ProtocolError(REJECT_INVALID, f"{area_id}: the view must be an object")
    unknown = set(view) - set(ROOM_FIELDS)
    if unknown:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: unknown fields {sorted(unknown)}")
    missing = [k for k, required in ROOM_FIELDS.items() if required and k not in view]
    if missing:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: missing fields {missing}")
    name = view["name"]
    if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: invalid name")
    if view["state"] not in STATES:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: invalid state {view['state']!r}")
    occupied = view["occupied"]
    if occupied is not None and not isinstance(occupied, bool):
        raise ProtocolError(REJECT_INVALID, f"{area_id}: occupied must be true, false or null")
    confidence = view["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: confidence must be 0-100")
    reason = view["reason"]
    if not isinstance(reason, str) or len(reason) > MAX_REASON:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: invalid reason")
    if not isinstance(view["reason_code"], str) or not _CODE.match(view["reason_code"]):
        raise ProtocolError(REJECT_INVALID, f"{area_id}: invalid reason code")
    if not isinstance(view["starting_up"], bool):
        raise ProtocolError(REJECT_INVALID, f"{area_id}: starting_up must be a boolean")
    problems = view["sensor_problems"]
    if isinstance(problems, bool) or not isinstance(problems, int) or not 0 <= problems <= 100:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: sensor_problems must be 0-100")
    if view["state"] == "vacant" and occupied is not False:
        raise ProtocolError(REJECT_INVALID, f"{area_id}: a vacant room must have occupied false")
    if view["state"] in ("possible_occupancy", "confirmed_occupancy", "possible_vacancy") and occupied is not True:
        if not (view["state"] == "possible_occupancy" and occupied is False):  # a door movement alone
            raise ProtocolError(REJECT_INVALID, f"{area_id}: state {view['state']} contradicts occupied")
    return {
        "name": name, "state": view["state"], "occupied": occupied, "confidence": confidence, "reason": reason,
        "reason_code": view["reason_code"], "last_activity": _time(view["last_activity"], "last_activity"),
        "state_since": _time(view["state_since"], "state_since"), "vacancy_at": _time(view["vacancy_at"], "vacancy_at"),
        "starting_up": view["starting_up"], "sensor_problems": problems,
    }


def check_snapshot(rooms: Any) -> dict[str, dict[str, Any]]:
    """The complete view of every enabled room, validated (raises ``ProtocolError``)."""
    if not isinstance(rooms, dict):
        raise ProtocolError(REJECT_INVALID, "rooms must be an object")
    if len(rooms) > MAX_ROOMS:
        raise ProtocolError(REJECT_INVALID, f"at most {MAX_ROOMS} rooms")
    return {area_id: check_room(area_id, view) for area_id, view in rooms.items()}


def check_disabled(disabled: Any, rooms: dict[str, Any]) -> dict[str, str]:
    """Configured rooms whose detection is turned off: area id -> name (their entities stay, unavailable)."""
    if disabled is None:
        return {}
    if not isinstance(disabled, dict) or len(disabled) + len(rooms) > MAX_ROOMS:
        raise ProtocolError(REJECT_INVALID, "disabled must be an object of room names")
    out = {}
    for area_id, name in disabled.items():
        if not valid_area_id(area_id) or area_id in rooms:
            raise ProtocolError(REJECT_INVALID, f"invalid disabled room {area_id!r}")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME:
            raise ProtocolError(REJECT_INVALID, f"{area_id}: invalid name")
        out[area_id] = name
    return out


def check_deleted(deleted: Any, rooms: dict[str, Any], disabled: dict[str, str]) -> list[str]:
    """Rooms an administrator deleted in the App: only these lose their entities. A room that is merely missing
    from a snapshot keeps its entities (unavailable), so a mistake can never delete entities."""
    if deleted is None:
        return []
    if not isinstance(deleted, list) or len(deleted) > MAX_ROOMS:
        raise ProtocolError(REJECT_INVALID, "deleted must be a list of room ids")
    for area_id in deleted:
        if not valid_area_id(area_id) or area_id in rooms or area_id in disabled:
            raise ProtocolError(REJECT_INVALID, f"invalid deleted room {area_id!r}")
    return list(dict.fromkeys(deleted))


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
