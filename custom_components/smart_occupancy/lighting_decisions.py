"""Explanations for automatic lighting decisions (0.3.7).

The lighting controller calls these at the points where it decides: after a
plan was executed (or evaluated without sending), after a gradual update,
when it registers a manual override, when a room becomes vacant. Each call
turns the controller's actual inputs (the plan and its per-light decisions,
the occupancy result, the daylight gate, the operating mode, the commands sent
and Home Assistant's errors) into one decision record (``decisions.py``).
Nothing here sends a command or changes a decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import DECISION_VERIFY_SECONDS, AutoOn, SensorKind
from .daylight import BRIGHT, DARK, GATE_CODES, GATE_NOT_NEEDED, GATE_UNCERTAIN
from .decisions import (
    BLOCKED,
    DISPATCHED,
    FAILED,
    MODULE_LIGHTING,
    NO_CHANGE,
    NOTED,
    OBSERVED,
    PARTIAL,
    Check,
    Decision,
    light_checks,
    minutes_text,
)
from .engine import RoomResult, SignalStatus, SignalView
from .occupancy import friendly_name
from .planner import CHANGE, SKIPPED, UNCHANGED, Plan, PlanItem

if TYPE_CHECKING:
    from .controller import LightingController

MODE_OBSERVING = "observing"
MODE_AUTOMATIC = "automatic"
MODE_PAUSED = "paused"

# Several state reports of one manual change (a group and its members) are one decision.
OVERRIDE_SAME_SECONDS = 5
ACTIVATION_TRIGGERS = frozenset({"motion", "occupancy_confirmed", "became_dark"})
TRIGGER_WORDS = {
    "motion": "motion", "occupancy_confirmed": "occupancy confirmed", "became_dark": "became dark",
    "mood_change": "mood changed", "room_mood_override": "room mood changed", "activated": "set to Automatic",
    "profile_change": "profile changed", "reconnected": "light available again", "adaptive": "global adjustment",
    "manual_change": "manual change", "vacant": "room vacant", "manual_mode": "Manual mood", "deactivated":
    "set to Observing",
}
_SOURCE_WORDS = {
    "ui": "from the Home Assistant app", "wall_switch": "with a wall switch", "unattributed": "by an unknown source",
    "automation": "by an automation",
}
_BLOCK_WORDS = {
    "offline": "{names} offline",
    "manual_override": "manual override on {names}",
    "conflict": "{names} also controlled by an automation that is not acknowledged",
    "occupancy_uncertain": "occupancy is uncertain",
    "off_needs_confirmation": "occupancy is not confirmed, so nothing is switched off",
    "manual_mode": "Main Mood is Manual",
}


def _plural(n: int, word: str = "light") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _names(items: list[str], limit: int = 3) -> str:
    """"A", "A and B", "A, B and C", "A, B, C and 2 more"."""
    items = list(dict.fromkeys(items))
    if len(items) > limit + 1:
        return f"{', '.join(items[:limit])} and {len(items) - limit} more"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _name(hass, entity_id: str) -> str:
    return friendly_name(hass, entity_id) or entity_id


def _plain(text: str) -> str:
    """"Natural light: Uncertain — no indoor light sensor" -> "no indoor light sensor"."""
    return text.split("—", 1)[1].strip() if "—" in text else text


def _until(iso: str | None) -> str:
    """"until 16:03" (Home Assistant's time zone), or until the room is vacant."""
    at = dt_util.parse_datetime(iso) if iso else None
    return f"until {dt_util.as_local(at).strftime('%H:%M')}" if at else "until the room is vacant"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _signal_phrase(view: SignalView) -> str:
    label = view.track.label()
    if view.status == SignalStatus.GRACE:
        return f"{label} cleared moments ago (still counted)"
    kind = view.track.kind
    if kind == SensorKind.PIR:
        return f"{label} detected motion"
    if kind in (SensorKind.RADAR, SensorKind.BED):
        return f"{label} detected presence"
    if kind == SensorKind.PC_ACTIVITY:
        return f"{label} activity detected"
    return f"{label} is active"


class LightingDecisions:
    """Builds and records the decisions of one lighting controller."""

    def __init__(self, ctrl: LightingController) -> None:
        self.ctrl = ctrl

    @property
    def log(self):
        return self.ctrl.decisions

    # --------------------------------------------------------------- context
    def mode(self, area_id: str) -> str:
        ctrl = self.ctrl
        if not ctrl.room_active(area_id):
            return MODE_OBSERVING
        return MODE_PAUSED if ctrl.manual_mode else MODE_AUTOMATIC

    def occupancy(self, area_id: str) -> tuple[str, dict[str, Any]]:
        """("room occupied: PIR detected motion", details) from the occupancy engine's current result."""
        result: RoomResult | None = self.ctrl.coordinator.occupancy.result(area_id)
        if result is None:
            return "occupancy is not known yet", {}
        holding = result.views(SignalStatus.ACTIVE) or result.views(SignalStatus.GRACE)
        holding = [v for v in holding if v.track.capable]
        details: dict[str, Any] = {
            "occupancy": result.state.value, "occupancy_reason": result.reason,
            "occupancy_inputs": [f"{v.track.label()}: {v.status.value}" for v in result.signals
                                 if v.status != SignalStatus.IGNORED],
        }
        newest = max(holding, key=lambda v: v.track.since or dt_util.utc_from_timestamp(0), default=None)
        if newest is not None:
            state = self.ctrl.hass.states.get(newest.track.entity_id)
            details["trigger_entity"] = newest.track.entity_id
            details["trigger_state"] = state.state if state else None
        if result.occupied and holding:
            return f"room occupied ({_names([_signal_phrase(v) for v in holding])})", details
        if result.occupied:
            return "room still occupied (vacancy is not confirmed yet)", details
        if result.occupied is False:
            return "room vacant", details
        return "occupancy is uncertain", details

    def _daylight(self, area_id: str, plan: Plan) -> tuple[str | None, dict[str, Any]]:
        gate = plan.daylight
        if gate is None:
            return None, {}
        status = self.ctrl.daylight.status(area_id)
        lux = status.estimate.lux
        details = {"daylight": gate.decision.value, "daylight_detail": gate.text, "on_below_lux": gate.on_below,
                   "auto_on": gate.policy.value, "lux": None if lux is None else round(lux)}
        if gate.policy == AutoOn.NEVER:
            return "automatic switch-on is off for this room", details
        if gate.policy == AutoOn.ALWAYS:
            return "switch-on regardless of daylight", details
        level = f"{round(lux)} lx, on below {gate.on_below} lx" if lux is not None else _plain(gate.text)
        if gate.decision == DARK:
            return f"dark ({level})", details
        if gate.decision == BRIGHT:
            return f"daylight is enough ({level})", details
        return f"daylight is uncertain ({_plain(gate.text)})", details

    def _why(self, area_id: str, trigger: str, plan: Plan, occupancy: str) -> str:
        mood = plan.mood
        if trigger in ("motion", "occupancy_confirmed"):
            return _cap(occupancy)
        if trigger == "became_dark":
            return "It became dark while the room is occupied"
        if trigger == "mood_change":
            return f"Mood changed to {mood}" if mood else "The mood changed"
        if trigger == "room_mood_override":
            return f"Room mood set to {mood}" if self.ctrl.lighting.room(area_id).mood_override else \
                f"Room mood returned to the global mood ({mood})"
        if trigger == "activated":
            return "Automatic lighting was switched on for this room"
        if trigger == "profile_change":
            return f"The {mood} lighting settings changed" if mood else "The lighting settings changed"
        if trigger == "reconnected":
            return "A light that was offline is available again"
        return f"Triggered by {TRIGGER_WORDS.get(trigger, trigger)}"

    def _blocked_reasons(self, items: list[PlanItem]) -> list[str]:
        by_code: dict[str, list[str]] = {}
        for item in items:
            by_code.setdefault(item.reason_code, []).append(item.name or item.entity_id)
        out: list[str] = []
        for code, names in by_code.items():
            if code == "vacant":
                out.append("the room is vacant (applied when someone arrives)")
            elif code in GATE_CODES:
                continue
            else:
                out.append(_BLOCK_WORDS.get(code, f"{code.replace('_', ' ')} ({{names}})").format(names=_names(names)))
        return out

    # ----------------------------------------------------------------- plans
    def plan(self, area_id: str, trigger: str, plan: Plan, relevant: list[PlanItem], *,
             ok: list[PlanItem] | None = None, errors: dict[str, str] | None = None,
             paused: set[str] | None = None) -> Decision | None:
        """Record the decision for one plan; ``ok`` is None when nothing was sent (evaluated only)."""
        ctrl = self.ctrl
        if not relevant:
            return None  # the room manages no light: there is nothing to decide
        rt = ctrl.runtime_for(area_id)
        mode = self.mode(area_id)
        if not plan.control_enabled:
            mode = MODE_OBSERVING
        elif plan.manual_mode:
            mode = MODE_PAUSED
        mood = plan.mood
        activation = trigger in ACTIVATION_TRIGGERS
        occupancy, details = self.occupancy(area_id)
        if not activation:
            # The occupancy sensor did not trigger this decision: keep the occupancy inputs only.
            details.pop("trigger_entity", None)
            details.pop("trigger_state", None)
        daylight, dl_details = self._daylight(area_id, plan)
        details.update(dl_details)
        details["mood"] = mood
        details["trigger"] = TRIGGER_WORDS.get(trigger, trigger)
        if trigger in ("mood_change", "room_mood_override"):
            details["mood_detail"] = ctrl.mood_status().detail
        if trigger == "mood_change" and (main := ctrl.config.mood.main_mood_entity):
            details["trigger_entity"] = main
            details["trigger_state"] = s.state if (s := ctrl.hass.states.get(main)) else None
        if trigger == "reconnected":
            details["trigger_entity"] = ", ".join(i.entity_id for i in relevant) or None
        overrides = [i for i in relevant if i.override]
        if overrides:
            details["manual_overrides"] = [
                f"{i.name or i.entity_id}: {i.override.get('source', 'manual')}, " + _until(i.override.get("until"))
                for i in overrides]
        details["targets"] = [f"{i.name or i.entity_id}: {i.decision} — {i.reason}" for i in relevant
                              if i.decision != SKIPPED]
        why = self._why(area_id, trigger, plan, occupancy)

        candidates = [i for i in relevant if i.service == "turn_on" and i.current.get("state") != "on"
                      and i.decision != SKIPPED]
        changes = [i for i in relevant if i.decision == CHANGE]
        blocked = [i for i in relevant if i.decision == "blocked"]
        unchanged = [i for i in relevant if i.decision == UNCHANGED]
        gated = [i for i in blocked if i.reason_code in GATE_CODES]
        other = [i for i in blocked if i.reason_code not in GATE_CODES]
        turning_on = [i for i in changes if i in candidates]
        light_clause = f", {daylight}" if activation and daylight and (turning_on or gated) else ""
        verb_action = (f"Restore {mood}" if trigger == "reconnected" else f"Update {mood}"
                       if trigger == "profile_change" else f"Apply {mood}")
        action = "Lights on" if activation and turning_on else verb_action
        reasons = self._blocked_reasons(other)
        tail = f" Not changed: {'; '.join(reasons)}." if reasons and changes else ""
        checks: list[Check] | None = None
        target = _names([i.name or i.entity_id for i in changes]) if changes else None
        codes = sorted({i.reason_code for i in blocked})

        if plan.manual_mode:
            status, action = BLOCKED, ("Lights on" if activation else "Apply mood")
            text = f"{why}, but Main Mood is Manual: automatic lighting is paused and nothing is sent."
        elif mood is None:
            status, action = BLOCKED, ("Lights on" if activation else "Apply mood")
            text = f"{why}, but there is no effective mood ({plan.note or 'Main Mood is unavailable'}): nothing is changed."
        elif ok is not None and changes:
            errors = errors or {}
            sent = [i for i in ok]
            failed = [i for i in changes if i.entity_id in errors]
            stopped = [i for i in changes if paused and i.entity_id in paused]
            text = f"{why}{light_clause}: {self._result_phrase(sent, mood)}."
            if failed:
                text += " Failed: " + "; ".join(f"{i.name or i.entity_id}: {errors[i.entity_id]}" for i in failed) + "."
            if stopped:
                text += f" {_plural(len(stopped))} not sent: Manual mood started."
            text += tail
            status = PARTIAL if failed and sent else FAILED if failed or not sent else DISPATCHED
            details["service"] = [f"light.{i.service} {i.name or i.entity_id} {i.data}" for i in changes]
            details["result"] = (f"{len(sent)} sent and accepted" + (f", {len(failed)} failed" if failed else "")
                                 + (f", {len(stopped)} not sent" if stopped else ""))
            if failed:
                details["errors"] = {i.entity_id: errors[i.entity_id] for i in failed}
            if status in (DISPATCHED, PARTIAL):
                checks = []
                for i in sent:
                    checks.extend(light_checks(i.service or "", {i.entity_id: i.data}))
                if status == PARTIAL:
                    checks = None  # a partial failure stays partial
        elif changes and not plan.control_enabled:
            status = OBSERVED
            text = f"{why}{light_clause}: would {self._result_phrase(changes, mood, would=True)}. " \
                   "Observing: nothing was sent." + tail
            details["service"] = [f"light.{i.service} {i.name or i.entity_id} {i.data}" for i in changes]
        elif activation and gated and not turning_on:
            code = gated[0].reason_code
            status = NO_CHANGE if code == GATE_NOT_NEEDED else BLOCKED
            action = "Keep lights off"
            if code == GATE_NOT_NEEDED:
                text = f"{why}, but {daylight}: lights stay off."
            elif code == GATE_UNCERTAIN:
                text = f"{why}, but {daylight} and this room then leaves lights off."
            else:
                text = f"{why}, but automatic switch-on is off for this room."
            if reasons:
                text += f" Also: {'; '.join(reasons)}."
        elif other:
            vacant_only = all(i.reason_code == "vacant" for i in other)
            status = NO_CHANGE if vacant_only else BLOCKED
            if activation and candidates:
                action = "Lights on"
            text = f"{why}: not changed — {'; '.join(reasons)}."
        elif unchanged:
            status = NO_CHANGE
            if activation:
                action = "Keep lights on" if any(i.current.get("state") == "on" for i in unchanged) else "No change"
            text = f"{why}: lighting already matches {mood}."
        else:
            status = NO_CHANGE
            action = "No change"
            text = f"{why}: no light is controlled in {mood}."

        key = ("plan", "activation" if activation else trigger, action, status, *codes)
        if activation and trigger != "became_dark" and rt.decision_visit == rt.visit and (
                status == NO_CHANGE or self.log.repeats(area_id, key)):
            # The same visit again (occupancy confirmed after the first motion): nothing new to log.
            return None
        if activation:
            rt.decision_visit = rt.visit
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger=trigger, kind="activation" if activation else trigger,
            action=action, explanation=text, mode=mode, status=status, target=target or mood, details=details,
            key=key, checks=checks, verify_after=self._verify_delay(changes),
        )

    @staticmethod
    def _verify_delay(items: list[PlanItem]) -> float:
        transition = max((float(i.data.get("transition") or 0) for i in items), default=0.0)
        return DECISION_VERIFY_SECONDS + transition

    @staticmethod
    def _result_phrase(items: list[PlanItem], mood: str | None, *, would: bool = False) -> str:
        on = [i for i in items if i.service == "turn_on" and i.current.get("state") != "on"]
        off = [i for i in items if i.service == "turn_off"]
        adjusted = [i for i in items if i not in on and i not in off]
        parts: list[str] = []
        if on:
            parts.append(f"switch on {_names([i.name or i.entity_id for i in on])}" if would
                         else f"{_names([i.name or i.entity_id for i in on])} switched on")
        if off:
            parts.append(f"switch off {_names([i.name or i.entity_id for i in off])}" if would
                         else f"{_names([i.name or i.entity_id for i in off])} switched off")
        if adjusted:
            parts.append(f"adjust {_names([i.name or i.entity_id for i in adjusted])}" if would
                         else f"{_names([i.name or i.entity_id for i in adjusted])} adjusted")
        if not parts:
            return "nothing to change"
        return "; ".join(parts) + (f" ({mood})" if mood else "")

    # ------------------------------------------------------------ other kinds
    def adaptive(self, area_id: str, sends: dict[str, dict[str, Any]], errors: dict[str, str],
                 forced: set[str]) -> Decision:
        """Gradual update to the global adjustment (lights already on only)."""
        ctrl = self.ctrl
        names = {e: _name(ctrl.hass, e) for e in sends}
        ok = [e for e in sends if e not in errors]
        brightness = sorted({d["brightness_pct"] for e, d in sends.items() if e in ok and "brightness_pct" in d})
        kelvin = sorted({d["color_temp_kelvin"] for e, d in sends.items() if e in ok and "color_temp_kelvin" in d})
        values = ", ".join(p for p in ("/".join(f"{b} %" for b in brightness), "/".join(f"{k} K" for k in kelvin)) if p)
        why = (f"{_names([names[e] for e in forced])} switched on by something else"
               if forced and set(sends) <= forced else "The global light adjustment changed")
        text = f"{why}: {_names([names[e] for e in ok]) if ok else 'no light'} set to {values or 'the current values'}."
        if errors:
            text += " Failed: " + "; ".join(f"{names[e]}: {m}" for e, m in errors.items()) + "."
        status = PARTIAL if errors and ok else FAILED if errors else DISPATCHED
        values_now = ctrl.global_values()
        details = {"trigger": TRIGGER_WORDS["adaptive"], "global_values": values_now.status,
                   "service": [f"light.turn_on {names[e]} {d}" for e, d in sends.items()],
                   "result": f"{len(ok)} sent and accepted" + (f", {len(errors)} failed" if errors else "")}
        if errors:
            details["errors"] = dict(errors)
        checks = None
        if status == DISPATCHED:
            checks = light_checks("turn_on", {e: d for e, d in sends.items() if e in ok})
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger="adaptive", kind="adaptive", action="Follow global adjustment",
            explanation=text, mode=self.mode(area_id), status=status, target=_names([names[e] for e in sends]),
            details=details, key=("adaptive", status), merge_seconds=None, checks=checks,
        )

    def override(self, area_id: str, targets: list[str], source: str, state: str, minutes: int | None, *,
                 switch: str | None = None, action: str | None = None) -> Decision:
        """A person's change is respected: automatic lighting leaves those lights alone."""
        ctrl = self.ctrl
        names = [_name(ctrl.hass, t) for t in targets]
        how = f"with wall switch {switch}" if switch else _SOURCE_WORDS.get(source, f"({source})")
        verb = "was" if len(names) == 1 else "were"
        text = (f"{_names(names)} {verb} switched {state} {how}: automatic lighting leaves "
                f"{'it' if len(names) == 1 else 'them'} as set {minutes_text(minutes)}.")
        details = {"trigger": TRIGGER_WORDS["manual_change"], "trigger_entity": ", ".join(targets),
                   "trigger_state": state, "override_source": source, "override_minutes": minutes}
        if switch:
            details["switch"] = switch
            if action:
                details["button"] = action
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger="manual_change", kind="override", action="Respect manual change",
            explanation=text, mode=self.mode(area_id), status=NOTED, target=_names(names), details=details,
            key=("override", *sorted(targets), source, state, switch or ""), same_seconds=OVERRIDE_SAME_SECONDS,
        )

    def vacant(self, area_id: str, lit: list[str], cleared: int) -> Decision | None:
        """The room became vacant: nothing is switched off automatically (no vacancy shutdown)."""
        if not lit and not cleared:
            return None
        ctrl = self.ctrl
        occupancy, details = self.occupancy(area_id)
        result = ctrl.coordinator.occupancy.result(area_id)
        reason = (result.reason.rstrip(".") if result else "no presence signal active")
        names = [_name(ctrl.hass, e) for e in lit]
        if lit:
            text = (f"{_cap(reason)}. {_names(names)} {'is' if len(names) == 1 else 'are'} left on: Smart Occupancy "
                    "does not switch lights off when a room becomes vacant.")
        else:
            text = f"{_cap(reason)}."
        if cleared:
            text += f" {_plural(cleared, 'manual override')} cleared: automatic lighting may change those lights again."
        details["trigger"] = TRIGGER_WORDS["vacant"]
        details["lights_on"] = names
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger="vacant", kind="vacancy",
            action="Leave lights on" if lit else "Clear manual overrides", explanation=text,
            mode=self.mode(area_id), status=NOTED if cleared else NO_CHANGE, target=_names(names) if names else None,
            details=details, key=("vacant", bool(lit), bool(cleared)),
        )

    def paused(self, area_id: str, triggers: set[str]) -> Decision:
        """Manual mood started while an application was queued: it is dropped, never replayed."""
        words = ", ".join(sorted(TRIGGER_WORDS.get(t, t) for t in triggers))
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger="manual_mode", kind="paused", action="Queued change dropped",
            explanation=f"Main Mood became Manual: the queued lighting change ({words}) was not sent.",
            mode=MODE_PAUSED, status=BLOCKED, details={"trigger": TRIGGER_WORDS["manual_mode"],
                                                        "trigger_entity": self.ctrl.config.mood.main_mood_entity},
            key=("paused",),
        )

    def deactivated(self, area_id: str) -> Decision:
        return self.log.record(
            area_id, module=MODULE_LIGHTING, trigger="deactivated", kind="mode", action="Set to Observing",
            explanation="Automatic lighting was set to Observing: decisions are still evaluated and logged, "
                        "no light is controlled.",
            mode=MODE_OBSERVING, status=NOTED, details={"trigger": TRIGGER_WORDS["deactivated"]}, key=("mode",),
        )
