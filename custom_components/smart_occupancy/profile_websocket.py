"""Websocket API of the Occupied and Vacant lighting profiles (0.4.0).

Reads are part of the room editor model (``smart_occupancy/lighting/editor``
with ``profile``, and the key ``vacant`` of a room). Writes are admin only
and go to the lighting store (no reload):

* ``vacant/set``: the Vacant profile's settings of a room (``area_id``) or
  the global defaults. A room value of ``null`` uses the global default.
* ``profile/copy``: copy a profile of one mood to another mood or profile (for
  example the Occupied profile into the Vacant one as a starting point). A
  room gets what the source gives its light types now (global and room
  values together) plus its single-light entries; the global level copies
  the global values.
* ``profile/reset``: remove the entries of one mood in one profile (a room's,
  or the global ones), so it inherits again.
* ``profile/test``: apply a profile now. The only command here that sends
  real light commands, and only when the user explicitly asks for it.

Editing never sends a light command: changes are used at the next trigger.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import ALL_MOODS, DOMAIN, LUX_MAX_THRESHOLD, LUX_MIN, RGB_SUFFIX, Profile, VacancyAction, VacantOthers
from .controller import LightingController
from .lighting_config import VacantDefaults, replace_mood_entries, table_get
from .profiles import as_entry, mood_chain, resolve
from .supervision import ControlNotPermittedError

_PROFILES = [p.value for p in Profile]
_MOOD = vol.All(str, vol.Length(min=1, max=100))
_LUX = vol.All(vol.Coerce(int), vol.Range(min=LUX_MIN, max=LUX_MAX_THRESHOLD))
_OTHERS_WORDS = {VacantOthers.OFF: "Turn off", VacantOthers.LEAVE: "Leave unchanged",
                 VacantOthers.MOOD: "Like a mood (lights that are on)"}


def _controller(hass: HomeAssistant, connection, msg) -> LightingController | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.lighting


def _moods_with_entries(ctrl: LightingController, area_id: str | None, profile: Profile) -> list[str]:
    """Moods that have entries of their own in one profile (the room's, or the global ones)."""
    lighting = ctrl.lighting
    if area_id is None:
        tables = [lighting.global_table(profile)]
    else:
        room = lighting.room(area_id)
        tables = [room.type_table(profile), room.light_table(profile)]
    out: dict[str, str] = {}
    for table in tables:
        for mood, entries in table.items():
            if entries and mood != ALL_MOODS:
                out.setdefault(mood.casefold(), mood)
    return sorted(out.values(), key=str.casefold)


def vacant_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    """The Vacant profile of one room: its settings, where each comes from, and its daylight decision now."""
    lighting = ctrl.lighting
    vacancy = lighting.room(area_id).vacancy
    glob = lighting.vacant
    status = ctrl.daylight.status(area_id)
    mood, _ = ctrl.room_mood(area_id)
    return {
        "action": vacancy.action.value,
        "used": vacancy.action == VacancyAction.VACANT_MOOD,
        "in_charge": ctrl.vacant_profile_room(area_id),
        "settings": vacancy.as_dict(),
        "global": glob.as_dict(),
        "effective": {"others": lighting.vacant_others(area_id).value, "on_below": lighting.vacant_on_below(area_id)},
        "sources": {"others": "room" if vacancy.others else "global",
                    "on_below": "room" if vacancy.on_below is not None else "global"},
        "others_words": {o.value: w for o, w in _OTHERS_WORDS.items()},
        "normal_on_below": status.config.on_below,
        "daylight": status.vacant_gate.as_dict() | {
            "committed": status.vacant_committed.value if status.vacant_committed else None,
            "off_above": status.vacant_off_above or status.config.off_above},
        "mood": mood,
        "has_entries_now": ctrl.has_vacant_profile(area_id, mood),
        "room_moods": _moods_with_entries(ctrl, area_id, Profile.VACANT),
        "occupied_room_moods": _moods_with_entries(ctrl, area_id, Profile.OCCUPIED),
        "limits": {"lux": [LUX_MIN, LUX_MAX_THRESHOLD]},
    }


# ------------------------------------------------------------------ settings
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/vacant/set",
    vol.Optional("area_id"): str,
    vol.Optional("others"): vol.Any(None, vol.In([o.value for o in VacantOthers])),
    vol.Optional("on_below"): vol.Any(None, _LUX),
    vol.Optional("mood"): vol.Any(None, vol.All(str, vol.Length(max=100))),
})
@websocket_api.require_admin
@callback
def ws_vacant_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The Vacant profile's settings of a room (``area_id``; ``null`` = global default) or the global defaults."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg.get("area_id")
    lighting = ctrl.lighting
    if area_id is None:
        if msg.get("others") == VacantOthers.MOOD.value or ("others" in msg and msg["others"] is None):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  "The global default is Turn off or Leave unchanged")
            return
        if "mood" in msg:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The mood is chosen per room")
            return
        current = lighting.vacant.as_dict()
        for key in ("others", "on_below"):
            if key in msg:
                current[key] = msg[key]
        ctrl.store.async_set(replace(lighting, vacant=VacantDefaults.from_dict(current)))
        connection.send_result(msg["id"], {"revision": ctrl.store.revision})
        return
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return
    current = lighting.room(area_id).vacancy
    changes: dict[str, Any] = {}
    if "mood" in msg:
        mood = (msg["mood"] or "").strip() or None
        if mood and (lighting.is_manual(mood) or mood == ALL_MOODS):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{mood} is not a mood")
            return
        changes["mood"] = mood
    if "others" in msg:
        changes["others"] = VacantOthers(msg["others"]) if msg["others"] else None
    if "on_below" in msg:
        changes["on_below"] = msg["on_below"]
    updated = replace(current, **changes)
    if updated.others == VacantOthers.MOOD and not updated.mood:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              "Choose the mood whose settings the other lights get")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, vacancy=updated))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


# ---------------------------------------------------------------- copy, reset
def _room_or_error(ctrl: LightingController, connection, msg) -> tuple[bool, str | None]:
    area_id = msg.get("area_id")
    if area_id is not None and area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return False, None
    return True, area_id


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/profile/copy",
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Required("from_profile"): vol.In(_PROFILES),
    vol.Required("from_mood"): _MOOD,
    vol.Required("to_profile"): vol.In(_PROFILES),
    vol.Required("to_mood"): _MOOD,
})
@websocket_api.require_admin
@callback
def ws_profile_copy(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Copy one mood's profile into another mood or profile (replaces the target's entries of that mood)."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    ok, area_id = _room_or_error(ctrl, connection, msg)
    if not ok:
        return
    src, dst = Profile(msg["from_profile"]), Profile(msg["to_profile"])
    from_mood, to_mood = msg["from_mood"].strip(), msg["to_mood"].strip()
    if ALL_MOODS in (from_mood, to_mood) or ctrl.lighting.is_manual(to_mood):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Choose a mood to copy from and to")
        return
    if src == dst and from_mood.casefold() == to_mood.casefold():
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Source and target are the same")
        return
    lighting = ctrl.lighting
    types, lights = copied_entries(ctrl, area_id, src, from_mood, dst)
    ctrl.store.async_set(replace_mood_entries(lighting, to_mood, dst, area_id=area_id, types=types, lights=lights))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "types": len(types), "lights": len(lights)})


def copied_entries(ctrl: LightingController, area_id: str | None, src: Profile, mood: str, dst: Profile,
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """What a copy writes: per light type the source's values (global and room together), plus single lights."""
    from .websocket import _target_keys, _targets_for

    lighting = ctrl.lighting
    index = ctrl.index
    levels = ("global",) if area_id is None else ("global", "room")
    tables = [lighting.global_table(src)] + ([lighting.room(area_id).type_table(src)] if area_id else [])
    controlled = set(ctrl.managed_room(area_id).controlled) if area_id else set()
    own = table_get(lighting.room(area_id).type_table(src), mood) if area_id else {}
    types: dict[str, Any] = {}
    for target in _targets_for(ctrl, mood, tables):
        if area_id and target not in own and not controlled & set(index.target_lights(target)):
            continue  # a light type without lights in this room
        entry = as_entry(resolve(lighting, mood, area_id, None, _target_keys(target), levels=levels, profile=src),
                         dst)
        if entry is not None and not (area_id is None and target.endswith(RGB_SUFFIX) and not entry.as_dict()):
            types[target] = entry
    lights: dict[str, Any] = {}
    if area_id:
        for entity_id, setting in table_get(lighting.room(area_id).light_table(src), mood).items():
            entry = replace(setting, persistent=None) if dst == Profile.VACANT else setting
            if not entry.is_empty:
                lights[entity_id] = entry
    return types, lights


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/profile/reset",
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Required("profile"): vol.In(_PROFILES),
    vol.Required("mood"): _MOOD,
})
@websocket_api.require_admin
@callback
def ws_profile_reset(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Remove every entry of one mood in one profile (the room's, or the global ones): it inherits again."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    ok, area_id = _room_or_error(ctrl, connection, msg)
    if not ok:
        return
    profile, mood = Profile(msg["profile"]), msg["mood"].strip()
    ctrl.store.async_set(replace_mood_entries(ctrl.lighting, mood, profile, area_id=area_id))
    inherits = [m for m in mood_chain(ctrl.lighting, mood)[1:]]
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "inherits_from": inherits})


# ----------------------------------------------------------------------- test
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/profile/test",
    vol.Required("area_id"): str,
    vol.Required("profile"): vol.In(_PROFILES),
    vol.Required("mood"): _MOOD,
    # The card asks the user first; the command refuses to run without this explicit confirmation.
    vol.Required("confirm"): True,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_profile_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Apply a profile now: sends real light commands (an explicit user action). The lights keep it as a
    manual override."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id, mood = msg["area_id"], msg["mood"].strip()
    if area_id not in ctrl.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
        return
    if mood == ALL_MOODS or ctrl.lighting.is_manual(mood):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{mood} is not a mood")
        return
    try:
        result = await ctrl.async_test_profile(area_id, Profile(msg["profile"]), mood, connection.user.id)
    except ControlNotPermittedError as err:
        connection.send_error(msg["id"], "not_permitted", str(err))
        return
    connection.send_result(msg["id"], result)


PROFILE_HANDLERS = (ws_vacant_set, ws_profile_copy, ws_profile_reset, ws_profile_test)

__all__ = ["PROFILE_HANDLERS", "copied_entries", "vacant_model"]
