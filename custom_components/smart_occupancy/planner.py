"""Mood lighting command plans.

Pure logic: given the resolved profiles, each target's capabilities and
current state, the room's occupancy and the protections in force, decide
what would be sent to every managed light or group, and why. Building a
plan never sends anything; the controller executes the ``change`` items of
a plan only in rooms whose lighting control was explicitly enabled.

Decision for each target, in order:

1. ``skipped`` - excluded, controlled through a managed group that contains
   it, or the profile resolves to Ignore (no command).
2. ``blocked`` - offline, a manual override is active, an enabled
   automation that was not acknowledged also controls it, or occupancy does
   not allow it (vacant or uncertain room; Off while occupancy is not yet
   confirmed). *Persistent* (ambient) settings ignore occupancy.
3. ``unchanged`` - the light already is in the requested state, or the
   same command was sent and nobody changed the light since.
4. ``change`` - the command that will be (or, in Observe mode, would be) sent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    BRIGHTNESS_TOLERANCE_PCT,
    HS_TOLERANCE,
    KELVIN_TOLERANCE,
    RGB_TOLERANCE,
    LightAction,
    OccupancyState,
)
from .lighting import Capabilities
from .profiles import ResolvedSetting

CHANGE = "change"
UNCHANGED = "unchanged"
BLOCKED = "blocked"
SKIPPED = "skipped"

_FIELD_WORDS = {
    "action": "action",
    "brightness_pct": "brightness",
    "color_temp_kelvin": "colour temperature",
    "rgb_color": "RGB colour",
    "hs_color": "HS colour",
    "effect": "effect",
    "transition": "transition",
    "persistent": "ambient",
}


@dataclass(slots=True)
class Target:
    """One managed light or group, as the planner sees it."""

    entity_id: str
    name: str | None
    category: str | None
    category_name: str | None
    state: str | None
    attributes: Mapping[str, Any]
    capabilities: Capabilities
    is_group: bool = False
    members: tuple[str, ...] = ()
    covered_by: str | None = None
    excluded: str | None = None
    override: dict[str, Any] | None = None
    conflicts: tuple[dict[str, Any], ...] = ()
    acknowledged_conflicts: tuple[dict[str, Any], ...] = ()
    last_command: tuple[str, dict[str, Any]] | None = None

    @property
    def offline(self) -> bool:
        return self.state is None or self.state in ("unavailable", "unknown")

    def current(self) -> dict[str, Any]:
        a = self.attributes
        brightness = a.get("brightness")
        out: dict[str, Any] = {"state": self.state}
        if self.state == "on":
            if isinstance(brightness, (int, float)):
                out["brightness_pct"] = round(brightness / 255 * 100)
            if a.get("color_mode") == "color_temp" and a.get("color_temp_kelvin"):
                out["color_temp_kelvin"] = a.get("color_temp_kelvin")
            elif a.get("rgb_color"):
                out["rgb_color"] = list(a["rgb_color"])
            if a.get("effect") not in (None, "", "none", "off"):
                out["effect"] = a.get("effect")
        return out


@dataclass(slots=True)
class PlanItem:
    entity_id: str
    name: str | None
    category: str | None
    category_name: str | None
    is_group: bool
    current: dict[str, Any]
    resolved: ResolvedSetting
    requested: dict[str, Any]
    service: str | None
    data: dict[str, Any]
    decision: str
    reason_code: str
    reason: str
    unsupported: list[dict[str, str]] = field(default_factory=list)
    persistent: bool = False
    override: dict[str, Any] | None = None
    conflicts: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "category": self.category,
            "category_name": self.category_name,
            "is_group": self.is_group,
            "current": self.current,
            "requested": self.requested,
            "resolved": self.resolved.as_dict(),
            "service": self.service,
            "data": self.data,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "unsupported": list(self.unsupported),
            "persistent": self.persistent,
            "override": self.override,
            "conflicts": [dict(c) for c in self.conflicts],
        }


@dataclass(slots=True)
class Plan:
    area_id: str
    mood: str | None
    occupancy_state: str | None
    occupied: bool | None
    trigger: str
    control_enabled: bool
    items: list[PlanItem]
    note: str | None = None

    @property
    def changes(self) -> list[PlanItem]:
        return [i for i in self.items if i.decision == CHANGE]

    def counts(self) -> dict[str, int]:
        out = {CHANGE: 0, UNCHANGED: 0, BLOCKED: 0, SKIPPED: 0}
        for item in self.items:
            out[item.decision] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "mood": self.mood,
            "occupancy_state": self.occupancy_state,
            "occupied": self.occupied,
            "trigger": self.trigger,
            "control_enabled": self.control_enabled,
            "will_execute": self.control_enabled and bool(self.changes),
            "note": self.note,
            "counts": self.counts(),
            "items": [i.as_dict() for i in self.items],
        }


def _source_text(source: dict[str, str] | None, mood: str | None) -> str:
    if not source:
        return "default"
    level = source.get("level")
    if level == "global":
        m = source.get("mood")
        return f"global {m}" + ("" if (m or "").casefold() == (mood or "").casefold() else " (inherited)")
    return "room override" if level == "room" else "light override"


def build_command(
    resolved: ResolvedSetting, caps: Capabilities
) -> tuple[str | None, dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    """(service, service data, requested state, unsupported notes) for a setting."""
    unsupported: list[dict[str, str]] = []

    def note(field_name: str, message: str, level: str = "warning") -> None:
        # "warning": the setting is dropped; "info": applied in a slightly different way.
        unsupported.append({"field": field_name, "message": message, "level": level})

    if resolved.action == LightAction.OFF:
        data: dict[str, Any] = {}
        if resolved.transition is not None:
            if caps.transition:
                data["transition"] = resolved.transition
            else:
                note("transition", "transition not supported; switched without it", "info")
        return "turn_off", data, {"state": "off", **data}, unsupported
    if resolved.action != LightAction.ON:
        return None, {}, {}, unsupported

    data = {}
    if (b := resolved.brightness_pct) is not None:
        if caps.brightness:
            data["brightness_pct"] = b
        else:
            note("brightness_pct", "brightness not supported (on/off only); brightness omitted")
    if (k := resolved.color_temp_kelvin) is not None:
        if caps.color_temp:
            low, high = caps.min_kelvin, caps.max_kelvin
            clamped = min(max(k, low or k), high or k)
            if clamped != k:
                note("color_temp_kelvin", f"{k} K is outside the light's range ({low}-{high} K); using {clamped} K",
                     "info")
            data["color_temp_kelvin"] = clamped
        elif caps.color:
            data["color_temp_kelvin"] = k
            note("color_temp_kelvin", "no white colour temperature; Home Assistant approximates it with a colour",
                 "info")
        else:
            note("color_temp_kelvin", "colour temperature not supported; omitted")
    for f, word in (("rgb_color", "RGB colour"), ("hs_color", "HS colour")):
        value = getattr(resolved, f)
        if value is None:
            continue
        if caps.color:
            data[f] = list(value)
        else:
            note(f, f"{word} not supported; omitted")
    if (e := resolved.effect) is not None:
        if e in caps.effects:
            data["effect"] = e
        elif caps.effects:
            note("effect", f"effect '{e}' is not offered (available: {', '.join(caps.effects)}); omitted")
        else:
            note("effect", "effects not supported; omitted")
    if (t := resolved.transition) is not None:
        if caps.transition:
            data["transition"] = t
        else:
            note("transition", "transition not supported; switched without it", "info")
    return "turn_on", data, {"state": "on", **data}, unsupported


def _close(a: Any, b: Any, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def matches(target: Target, service: str, data: dict[str, Any]) -> bool:
    """Is the light already in the requested state?"""
    a = target.attributes
    if service == "turn_off":
        return target.state == "off"
    if target.state != "on":
        return False
    if "brightness_pct" in data:
        brightness = a.get("brightness")
        if not isinstance(brightness, (int, float)) or not _close(
            brightness / 255 * 100, data["brightness_pct"], BRIGHTNESS_TOLERANCE_PCT
        ):
            return False
    if "color_temp_kelvin" in data:
        if not target.capabilities.color_temp:
            return False  # emulated with a colour: cannot compare
        if a.get("color_mode") != "color_temp" or not _close(
            a.get("color_temp_kelvin"), data["color_temp_kelvin"], KELVIN_TOLERANCE
        ):
            return False
    if "rgb_color" in data:
        rgb = a.get("rgb_color")
        if not rgb or not all(_close(x, y, RGB_TOLERANCE) for x, y in zip(rgb, data["rgb_color"], strict=False)):
            return False
    if "hs_color" in data:
        hs = a.get("hs_color")
        if not hs or not all(_close(x, y, HS_TOLERANCE) for x, y in zip(hs, data["hs_color"], strict=False)):
            return False
    return not ("effect" in data and a.get("effect") != data["effect"])


def describe(resolved: ResolvedSetting, mood: str | None) -> str:
    """Plain-language summary of a resolved setting and where its values come from."""
    if resolved.action == LightAction.OFF:
        return f"Off ({_source_text(resolved.sources.get('action'), mood)})"
    if resolved.action != LightAction.ON:
        return "Ignore"
    parts: list[str] = []
    if resolved.brightness_pct is not None:
        parts.append(f"{resolved.brightness_pct} %")
    if resolved.color_temp_kelvin is not None:
        parts.append(f"{resolved.color_temp_kelvin} K")
    if resolved.rgb_color is not None:
        parts.append("RGB " + ",".join(str(c) for c in resolved.rgb_color))
    if resolved.hs_color is not None:
        parts.append("HS " + ",".join(f"{c:g}" for c in resolved.hs_color))
    if resolved.effect:
        parts.append(f"effect {resolved.effect}")
    head = "On" + (f" {', '.join(parts)}" if parts else "")
    by_source: dict[str, list[str]] = {}
    for f, src in resolved.sources.items():
        if f in ("transition",):
            continue
        by_source.setdefault(_source_text(src, mood), []).append(_FIELD_WORDS.get(f, f))
    origin = "; ".join(f"{', '.join(fields)} from {src}" for src, fields in by_source.items())
    return f"{head} ({origin})" if origin else head


def build_plan(
    *,
    area_id: str,
    mood: str | None,
    targets: list[Target],
    resolve_fn,
    occupancy_state: str | None,
    occupied: bool | None,
    trigger: str,
    control_enabled: bool,
    note: str | None = None,
) -> Plan:
    """Build the plan for one room. ``resolve_fn(target) -> ResolvedSetting``."""
    items: list[PlanItem] = []
    confirmed = occupancy_state == OccupancyState.CONFIRMED_OCCUPANCY
    for t in targets:
        resolved = resolve_fn(t) if mood else ResolvedSetting()
        service, data, requested, unsupported = build_command(resolved, t.capabilities)
        item = PlanItem(
            entity_id=t.entity_id, name=t.name, category=t.category, category_name=t.category_name,
            is_group=t.is_group, current=t.current(), resolved=resolved, requested=requested,
            service=service, data=data, decision=SKIPPED, reason_code="", reason="",
            unsupported=unsupported, persistent=resolved.is_persistent, override=t.override,
            conflicts=t.conflicts,
        )
        items.append(item)
        summary = describe(resolved, mood)

        def set_(decision: str, code: str, reason: str, item: PlanItem = item) -> None:
            item.decision, item.reason_code, item.reason = decision, code, reason

        if mood is None:
            set_(SKIPPED, "no_mood", "No effective mood, so nothing is changed")
            continue
        if t.excluded:
            set_(SKIPPED, "excluded", f"Excluded from automatic management ({t.excluded})")
            continue
        if t.covered_by:
            set_(SKIPPED, "covered_by_group",
                 f"Member of managed group {t.covered_by}; controlled through the group to avoid duplicate commands")
            continue
        if service is None:
            why = "the profile says Ignore" if resolved.sources.get("action") else "no profile sets an action"
            set_(SKIPPED, "ignored", f"Not controlled in {mood}: {why}")
            continue
        if t.offline:
            set_(BLOCKED, "offline", f"Light is {t.state or 'missing'}; nothing can be sent ({summary})")
            continue
        if t.override:
            until = t.override.get("until")
            source = t.override.get("source", "manual")
            set_(BLOCKED, "manual_override",
                 f"Manual change ({source}) is respected" + (f" until {until}" if until else " until the room is vacant"))
            continue
        if t.conflicts:
            names = ", ".join(c.get("name") or c["automation"] for c in t.conflicts)
            set_(BLOCKED, "conflict",
                 f"Also controlled by enabled automation(s) {names}; acknowledge or resolve the conflict first")
            continue
        if not resolved.is_persistent:
            if occupancy_state in (None, OccupancyState.UNCERTAIN):
                set_(BLOCKED, "occupancy_uncertain", f"Occupancy is uncertain; nothing is changed ({summary})")
                continue
            if not occupied:
                set_(BLOCKED, "vacant",
                     f"Room is not occupied; applied when occupancy is next confirmed ({summary})")
                continue
            if service == "turn_off" and not confirmed:
                set_(BLOCKED, "off_needs_confirmation",
                     "Occupancy is not confirmed; lights are not switched off on uncertain evidence")
                continue
        if matches(t, service, data):
            set_(UNCHANGED, "already", f"Already in the requested state: {summary.split(' (')[0]}")
            continue
        if t.last_command == (service, data):
            set_(UNCHANGED, "already_sent", "The same command was already sent and the light was not changed since")
            continue
        verb = "Will" if control_enabled else "Would"
        why = {
            "mood_change": f"the mood changed to {mood}",
            "occupancy_confirmed": "occupancy was just confirmed",
            "room_mood_override": f"the room mood was set to {mood}",
            "profile_change": "the profile was changed",
            "reconnected": "the light is available again",
            "preview": f"it does not match {mood}",
        }.get(trigger, trigger)
        ambient = " Ambient: applied regardless of occupancy." if resolved.is_persistent and not occupied else ""
        set_(CHANGE, "change", f"{verb} set {summary} because {why}.{ambient}")
    return Plan(area_id, mood, occupancy_state, occupied, trigger, control_enabled, items, note)
