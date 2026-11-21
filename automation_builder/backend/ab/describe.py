"""Readable summaries of rules: "IF ... AND ... THEN ..."."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import catalog
from .model import action_entities

Names = Callable[[str], str]
DAY = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"}
DATA_LABELS = {"brightness_pct": ("brightness", " %"), "color_temp_kelvin": ("colour temperature", " K"),
               "transition": ("transition", " s"), "percentage": ("speed", " %"), "position": ("position", " %"),
               "temperature": ("", "°"), "hvac_mode": ("", ""), "preset_mode": ("", ""), "option": ("", ""),
               "humidity": ("", " %"), "value": ("", ""), "volume_level": ("volume", "")}


def duration(seconds: int) -> str:
    seconds = int(seconds)
    parts = []
    for size, unit in ((3600, "h"), (60, "min"), (1, "s")):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {unit}")
    return " ".join(parts) or "0 s"


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _days(days: list[str]) -> str:
    if not days:
        return ""
    if days == ["mon", "tue", "wed", "thu", "fri"]:
        return " on weekdays"
    if days == ["sat", "sun"]:
        return " at weekends"
    return " on " + ", ".join(DAY[d] for d in days)


def _hm(value: str) -> str:
    return value[:5] if value.endswith(":00") else value


def trigger(t: dict[str, Any], names: Names) -> str:
    kind = t["type"]
    if kind == "state":
        text = names(t["entity_id"])
        if t["from"] is not None and t["to"] is not None:
            text += f" changes from “{t['from']}” to “{t['to']}”"
        elif t["to"] is not None:
            text += f" changes to “{t['to']}”"
        elif t["from"] is not None:
            text += f" changes from “{t['from']}”"
        else:
            text += " changes"
        return text + (f" and stays so for {duration(t['for_seconds'])}" if t["for_seconds"] else "")
    if kind == "numeric":
        text = names(t["entity_id"])
        if t["above"] is not None and t["below"] is not None:
            text += f" comes between {_num(t['above'])} and {_num(t['below'])}"
        elif t["above"] is not None:
            text += f" rises above {_num(t['above'])}"
        else:
            text += f" drops below {_num(t['below'])}"
        return text + (f" and stays so for {duration(t['for_seconds'])}" if t["for_seconds"] else "")
    if kind == "time":
        return f"the time is {_hm(t['at'])}{_days(t['weekdays'])}"
    if kind == "sun":
        offset = t["offset_minutes"]
        when = f"{duration(abs(offset) * 60)} {'before' if offset < 0 else 'after'} " if offset else "at "
        return f"it is {when}{t['event']}"
    if kind == "occupancy":
        return f"{names(t['entity_id'])} becomes {t['to']}"
    return kind


def condition(c: dict[str, Any], names: Names, top: bool = False) -> str:
    kind = c["type"]
    if kind in ("and", "or"):
        text = f" {kind.upper()} ".join(condition(i, names) for i in c["items"])
        return text if top or len(c["items"]) < 2 else f"({text})"
    if kind == "state":
        text = f"{names(c['entity_id'])} is {'not ' if c['negate'] else ''}“{c['state']}”"
        return text + (f" for {duration(c['for_seconds'])}" if c["for_seconds"] else "")
    if kind == "numeric":
        name = names(c["entity_id"])
        if c["above"] is not None and c["below"] is not None:
            return f"{name} is between {_num(c['above'])} and {_num(c['below'])}"
        return f"{name} is above {_num(c['above'])}" if c["above"] is not None else f"{name} is below {_num(c['below'])}"
    if kind == "time":
        if c["after"] and c["before"]:
            text = f"the time is between {_hm(c['after'])} and {_hm(c['before'])}"
        elif c["after"]:
            text = f"the time is after {_hm(c['after'])}"
        elif c["before"]:
            text = f"the time is before {_hm(c['before'])}"
        else:
            return f"it is{_days(c['weekdays'])[3:] and ' ' + _days(c['weekdays']).strip()}"
        return text + _days(c["weekdays"])
    return kind


def _data(data: dict[str, Any]) -> str:
    parts = []
    for key, value in data.items():
        label, unit = DATA_LABELS.get(key, (key.replace("_", " "), ""))
        shown = _num(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
        parts.append(f"{label + ' ' if label else ''}{shown}{unit}")
    return f" ({', '.join(parts)})" if parts else ""


def action(a: dict[str, Any], names: Names, targets: list[str]) -> str:
    if a["type"] == "delay":
        return f"wait {duration(a['seconds'])}"
    domain, _, service = a["action"].partition(".")
    preset = next((p for p in catalog.DOMAINS.get(domain, {}).get("actions", []) if p["service"] == service), None)
    entities = action_entities(a, targets)
    who = ", ".join(names(e) for e in entities)
    if preset is None:
        return f"run {a['action']}{' on ' + who if who else ''}{_data(a['data'])}"
    verb = preset["label"][0].lower() + preset["label"][1:]
    head, _, tail = verb.partition(" ")
    if head == "turn" and who:
        return f"turn {who} {tail}{_data(a['data'])}"
    if head == "set" and who:
        return f"set the {tail} of {who}{_data(a['data'])}"
    return f"{verb} {who}{_data(a['data'])}".strip()


def rule(r: dict[str, Any], names: Names, targets: list[str]) -> dict[str, str]:
    """``{"if": ..., "and": ..., "then": ..., "text": ...}`` for one rule."""
    if_text = " OR ".join(trigger(t, names) for t in r["triggers"]) or "…"
    and_text = condition(r["conditions"], names, top=True) if r["conditions"]["items"] else ""
    then_text = ", then ".join(action(a, names, targets) for a in r["actions"]) or "…"
    text = f"IF {if_text}" + (f" AND {and_text}" if and_text else "") + f" THEN {then_text}"
    return {"if": if_text, "and": and_text, "then": then_text, "text": text}
