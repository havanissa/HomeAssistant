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
3. ``blocked`` by the daylight gate (0.3.3) - switching an *off* light on is
   not wanted: daylight is enough, daylight is uncertain and the room then
   leaves lights off, or automatic switch-on is off for the room. Lights that
   are already on still follow the mood, Off is never affected, and ambient
   (persistent) settings are never gated.
4. ``unchanged`` - the light already is in the requested state, or the
   same command was sent and nobody changed the light since.
5. ``change`` - the command that will be (or, in Observe mode, would be) sent.

Brightness and colour temperature that follow the global adjustment are
already filled in (``adaptive.apply_global``). While Main Mood is the Manual
option, every target is ``blocked`` (``manual_mode``): nothing is sent.

Vacancy plans (0.3.8, ``build_vacancy_plan``) decide what happens to the
room's lights when it has become vacant, by the room's vacancy action:

* ``turn_off``: lights that are on are switched off;
* ``vacant_mood``: the chosen mood's profile is applied to the lights that
  are on (On with its brightness and colour, Off, or left unchanged); it
  never switches an off light on in an empty room;
* ``leave``: nothing is planned.

Ambient (persistent) settings of the room's current mood are never touched
by vacancy, and the same protections as for mood plans apply first
(exclusions, groups, offline lights, manual overrides, automation conflicts,
Manual mode). A light that already is in the vacancy state is ``unchanged``
even while it has a manual override.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    BRIGHTNESS_TOLERANCE_PCT,
    HS_TOLERANCE,
    KELVIN_TOLERANCE,
    MANUAL_PAUSED_TEXT,
    RGB_TOLERANCE,
    AdjustMode,
    AutoOn,
    LightAction,
    OccupancyState,
    VacancyAction,
)
from .daylight import DaylightGate
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
    # Profile targets of the light, least specific first.
    targets: tuple[str, ...] = ()

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
    # The next gradual (global adjustment) update of this light, set by the controller.
    adaptive_update: dict[str, Any] | None = None

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
            "adaptive": self.resolved.adaptive,
            "adaptive_update": self.adaptive_update,
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
    manual_mode: bool = False
    daylight: DaylightGate | None = None
    # Vacancy plans (0.3.8): the room's vacancy action, and the mood whose ambient lights are kept.
    vacancy: str | None = None
    current_mood: str | None = None

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
            "manual_mode": self.manual_mode,
            "daylight": self.daylight.as_dict() if self.daylight else None,
            "vacancy": self.vacancy,
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
    following_b = resolved.brightness_mode in (AdjustMode.GLOBAL, AdjustMode.OFFSET)
    following_k = resolved.color_temp_mode in (AdjustMode.GLOBAL, AdjustMode.OFFSET)
    if (b := resolved.brightness_pct) is not None:
        if caps.brightness:
            data["brightness_pct"] = b
        elif following_b:
            note("brightness_pct", "on/off only; the global brightness is not used", "info")
        else:
            note("brightness_pct", "brightness not supported (on/off only); brightness omitted")
    for kind, f in (("brightness", "brightness_pct"), ("color_temp", "color_temp_kelvin")):
        entry = (resolved.adaptive or {}).get(kind) or {}
        if entry.get("reason") and entry.get("value") is None:
            note(f, entry["reason"], "info")  # the source problem itself is reported once, not per light
    if (k := resolved.color_temp_kelvin) is not None and following_k and not caps.color_temp:
        # A global white temperature is applied only to lights with a native white mode.
        note("color_temp_kelvin", "no colour temperature mode; follows the global brightness only"
             if caps.brightness else "no colour temperature mode; the global colour temperature is not used", "info")
    elif k is not None:
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
    glob = {AdjustMode.GLOBAL: " (global)", AdjustMode.OFFSET: " (global + offset)"}
    if resolved.brightness_pct is not None:
        parts.append(f"{resolved.brightness_pct} %{glob.get(resolved.brightness_mode, '')}")
    if resolved.color_temp_kelvin is not None:
        parts.append(f"{resolved.color_temp_kelvin} K{glob.get(resolved.color_temp_mode, '')}")
    if resolved.rgb_color is not None:
        parts.append("RGB " + ",".join(str(c) for c in resolved.rgb_color))
    if resolved.hs_color is not None:
        parts.append("HS " + ",".join(f"{c:g}" for c in resolved.hs_color))
    if resolved.effect:
        parts.append(f"effect {resolved.effect}")
    head = "On" + (f" {', '.join(parts)}" if parts else "")
    by_source: dict[str, list[str]] = {}
    for f, src in resolved.sources.items():
        if f in ("transition", "brightness_offset", "color_temp_offset", "color_temp_mode", "brightness_mode"):
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
    manual_mode: bool = False,
    gate: DaylightGate | None = None,
) -> Plan:
    """Build the plan for one room. ``resolve_fn(target) -> ResolvedSetting``."""
    gate_block = gate.blocks() if gate else None
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

        if manual_mode:
            set_(BLOCKED, "manual_mode", f"{MANUAL_PAUSED_TEXT}: nothing is sent")
            continue
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
            if gate_block and service == "turn_on" and t.state != "on":
                set_(BLOCKED, gate_block[0], gate_block[1])
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
            "adaptive": "the global adjustment changed",
            "motion": "motion was detected",
            "became_dark": "the room became dark",
            "preview": f"it does not match {mood}",
        }.get(trigger, trigger)
        ambient = " Ambient: applied regardless of occupancy." if resolved.is_persistent and not occupied else ""
        light = ""
        if gate and gate.policy == AutoOn.DARK and service == "turn_on" and t.state != "on" \
                and not resolved.is_persistent:
            light = f" Daylight: {gate.decision.value} ({gate.text})."
        set_(CHANGE, "change", f"{verb} set {summary} because {why}.{ambient}{light}")
    return Plan(area_id, mood, occupancy_state, occupied, trigger, control_enabled, items, note, manual_mode, gate)


def build_vacancy_plan(
    *,
    area_id: str,
    action: VacancyAction,
    mood: str | None,
    vacant_mood: str | None,
    targets: list[Target],
    resolve_current,
    resolve_vacant,
    occupancy_state: str | None,
    occupied: bool | None,
    trigger: str,
    control_enabled: bool,
    note: str | None = None,
    manual_mode: bool = False,
) -> Plan:
    """What the room's vacancy action does to each target (see the module docstring). Never sends anything.

    ``mood`` is the room's current mood (its ambient lights are kept);
    ``resolve_current(target)`` / ``resolve_vacant(target)`` resolve the
    current and the vacant mood for a target.
    """
    items: list[PlanItem] = []
    plan_mood = vacant_mood if action == VacancyAction.VACANT_MOOD else mood
    for t in targets:
        current = resolve_current(t) if mood else ResolvedSetting()
        resolved = resolve_vacant(t) if action == VacancyAction.VACANT_MOOD and vacant_mood else ResolvedSetting()
        if action == VacancyAction.TURN_OFF:
            service, data, requested, unsupported = "turn_off", {}, {"state": "off"}, []
        elif action == VacancyAction.VACANT_MOOD and vacant_mood:
            service, data, requested, unsupported = build_command(resolved, t.capabilities)
        else:
            service, data, requested, unsupported = None, {}, {}, []
        item = PlanItem(
            entity_id=t.entity_id, name=t.name, category=t.category, category_name=t.category_name,
            is_group=t.is_group, current=t.current(), resolved=resolved, requested=requested,
            service=service, data=data, decision=SKIPPED, reason_code="", reason="",
            unsupported=unsupported, persistent=current.is_persistent, override=t.override,
            conflicts=t.conflicts,
        )
        items.append(item)

        def set_(decision: str, code: str, reason: str, item: PlanItem = item) -> None:
            item.decision, item.reason_code, item.reason = decision, code, reason

        if t.excluded:
            set_(SKIPPED, "excluded", f"Excluded from automatic management ({t.excluded})")
            continue
        if t.covered_by:
            set_(SKIPPED, "covered_by_group",
                 f"Member of managed group {t.covered_by}; controlled through the group to avoid duplicate commands")
            continue
        if action == VacancyAction.LEAVE:
            set_(SKIPPED, "vacancy_leave", "Vacancy behaviour is Leave unchanged")
            continue
        if manual_mode:
            set_(BLOCKED, "manual_mode", f"{MANUAL_PAUSED_TEXT}: nothing is sent")
            continue
        if mood is None:
            # Without a mood it is unknown which lights are ambient, so nothing is switched off.
            set_(BLOCKED, "no_mood", "No effective mood, so ambient lights cannot be told apart; nothing is changed")
            continue
        if action == VacancyAction.VACANT_MOOD and not vacant_mood:
            set_(BLOCKED, "no_vacant_mood", "No vacant mood is chosen for this room; nothing is changed")
            continue
        if current.is_persistent:
            set_(SKIPPED, "ambient", f"Ambient in {mood}: follows the mood whether or not the room is occupied")
            continue
        if t.offline:
            set_(BLOCKED, "offline", f"Light is {t.state or 'missing'}; it could not be changed")
            continue
        if service is None:
            set_(UNCHANGED, "vacant_ignore", f"Left as it is: the vacant mood {vacant_mood} does not set it")
            continue
        if service == "turn_off" and t.state == "off":
            set_(UNCHANGED, "already", "Already off")
            continue
        if service == "turn_on" and t.state != "on":
            set_(UNCHANGED, "stays_off", f"Stays off: the vacant mood {vacant_mood} does not switch lights on "
                 "in an empty room")
            continue
        if t.override:
            until = t.override.get("until")
            source = t.override.get("source", "manual")
            set_(BLOCKED, "manual_override",
                 f"Manual change ({source}) is respected" + (f" until {until}" if until else ""))
            continue
        if t.conflicts:
            names = ", ".join(c.get("name") or c["automation"] for c in t.conflicts)
            set_(BLOCKED, "conflict",
                 f"Also controlled by enabled automation(s) {names}; acknowledge or resolve the conflict first")
            continue
        if service == "turn_on" and matches(t, service, data):
            set_(UNCHANGED, "already", f"Already in the vacant mood {vacant_mood}")
            continue
        verb = "Will" if control_enabled else "Would"
        if service == "turn_off":
            why = "the room is vacant" if action == VacancyAction.TURN_OFF else f"the vacant mood {vacant_mood} sets Off"
            set_(CHANGE, "change", f"{verb} switch off because {why}.")
        else:
            set_(CHANGE, "change", f"{verb} set {describe(resolved, vacant_mood)} (vacant mood {vacant_mood}).")
    return Plan(area_id, plan_mood, occupancy_state, occupied, trigger, control_enabled, items, note, manual_mode,
                None, action.value, mood)
