"""Automatic curtain decisions (0.6.2, the user's Phase 4.6.1; solar and thermal 0.6.3, Phase 4.6.2).

Pure logic, no Home Assistant calls.

``decide()`` chooses one *condition* for a whole window from evidence and
returns the preset the window's condition mapping gives it, with a sentence
that explains why. The base conditions are tried in this order:

1. **Sleep**: sleep protection is on for the window and its sleep source is on.
2. **Night**: inside the household night (separate times before workdays and
   free days).
3. **Evening privacy**: the window has privacy on and it is dark outside in
   the evening (measured light below the dusk level, or the sun below the dusk
   elevation, so it also works without a light sensor).
4. **Waiting for daylight**: nothing above applies, but there is no evidence
   that allows opening (no confirmed daylight, a stale or unavailable sensor, an
   unavailable sleep source). The window keeps its position: **missing evidence
   never opens a window, and a clock time never does either.**
5. **Direct sunlight** (only for windows with sun protection on): the sun is
   in front of the window (geometry) *and* direct sunshine is confirmed —
   measured strong outdoor light, or (opt-in, only without a light sensor) a
   clear sky from the weather entity. Geometry alone is never proof of
   sunshine, a stuck or stale sensor is not either, and a forecast never
   overrides a working light sensor. With direct sunlight the window chooses,
   highest first (0.6.3; every step is opt-in except plain sun protection):

   a. **Excess solar heat** — temperature-aware sun on and the room above
      its maximum acceptable temperature (preset: Excess solar heat, else
      Direct sunlight);
   b. **Direct sunlight** (sun protection) — the room is warm (above the
      comfortable range), or temperature-aware sun is off, or the room
      temperature is unknown (then it decides exactly as in 0.6.2);
   c. **Glare protection** — a glare-sensitive window (optionally only while
      the room is occupied); it wins over useful solar heating;
   d. **Useful solar heating** — the room is cold, its warmth is welcome and
      it is not warm outside: the Morning / Normal daytime preset is kept;
   e. otherwise Direct sunlight.

6. **Morning**, then **Normal daytime**.

Two overlays sit above the base condition:

* a **manual hold** (a manual move, or a preset an administrator applied) keeps
  the covers as they are until its release policy ends it; protective conditions
  chosen in ``manual_bypass`` end a hold that began before they started;
* the **window lamp** applies its preset while the lamp is on (and for the
  release delay after it switches off), except over the conditions in
  ``lamp_bypass`` (``sun`` there also covers glare and excess heat).

Nothing thermal ever bypasses sleep, night, evening privacy or a manual hold:
those are decided before the sun is looked at.

0.6.4 (Global Curtain Modes): which preset a condition uses is no longer a
single window mapping but is resolved per cover from Global Modes, room,
window and cover settings (``modes.py``, passed in as ``Flags.targets``). The
arbitration here is unchanged; only "is this condition mapped?" and "what does
it send?" ask the resolver. A condition without an assignment keeps the covers
where they are; it never hands over to a lower condition.

Unavailable covers, competing automations, running movements, the minimum
interval and the daily budget are *safety* limits applied by the runtime
(``auto.py``) before anything is sent; they rank above everything here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import schedule
from .config import CONDITION_WORDS, MODE_WORDS, SUN_FALLBACK
from .modes import NOTHING, Effective, mapping_effective
# 0.6.5: the schedule lives in schedule.py (one evaluation for windows and the household); these names stay here.
from .schedule import at as _at, day_from, is_free, night_bounds, night_now  # noqa: F401

PROTECTIVE = ("sleep", "night", "evening_privacy")
SUN_BASES = frozenset({"sun", "glare", "heat", "solar_heating"})   # the sun is in front and shining
BAD_LUX = frozenset({"stale", "unavailable", "stuck", "implausible", "missing"})
PHASE_EXEMPT = frozenset({"waiting"})   # "waiting" is not a phase: it only keeps the position
DWELL_EXEMPT = frozenset({"sleep", "night", "evening_privacy", "lamp"})
BUDGET_EXEMPT = frozenset({"sleep", "night"})
MAX_LOOKBACK = timedelta(minutes=260)


# ------------------------------------------------------------- light history
class History:
    """State changes of one sensor (time, value or None when unavailable), newest last.

    ``since(test)`` answers "since when has every reading passed ``test``": the
    start of the newest unbroken run. Keeping changes (not samples) is enough,
    because Home Assistant reports a state only when it changes.
    """

    def __init__(self) -> None:
        self.items: list[tuple[datetime, float | None]] = []

    def add(self, when: datetime, value: float | None) -> None:
        if self.items and self.items[-1][1] == value:
            return
        self.items.append((when, value))
        cutoff = when - MAX_LOOKBACK
        while len(self.items) > 1 and self.items[1][0] <= cutoff:
            self.items.pop(0)

    @property
    def value(self) -> float | None:
        return self.items[-1][1] if self.items else None

    def since(self, test: Callable[[float], bool]) -> datetime | None:
        start = None
        for when, value in reversed(self.items):
            if value is None or not test(value):
                break
            start = when
        return start


@dataclass(slots=True)
class Hysteresis:
    """A yes/no state with separate on and off thresholds and hold times; unknown (None) until proven."""

    state: bool | None = None

    def evaluate(self, history: History, now: datetime, on: Callable[[float], bool], on_for: timedelta,
                 off: Callable[[float], bool], off_for: timedelta) -> tuple[bool | None, datetime | None]:
        """The state now and when it may change next by time alone."""
        on_since, off_since = history.since(on), history.since(off)
        if on_since is not None and now - on_since >= on_for:
            self.state = True
        elif off_since is not None and now - off_since >= off_for:
            self.state = False
        deadline = None
        if on_since is not None and self.state is not True:
            deadline = on_since + on_for
        elif off_since is not None and self.state is not False:
            deadline = off_since + off_for
        return self.state, deadline

    def reset(self) -> None:
        self.state = None


# ----------------------------------------------------------------- evidence
@dataclass(slots=True)
class Evidence:
    """What the runtime measured for one window (all times local and timezone-aware)."""

    now: datetime
    sleep: str | None = None          # "on" / "off" / "unknown"; None = sleep protection is off
    sleep_entity: str | None = None
    sleep_ended: datetime | None = None
    lamp: str | None = None           # "on" / "releasing" / "off"; None = no lamp override
    lamp_release_at: datetime | None = None
    lux: float | None = None
    lux_status: str = "none"          # ok / stale / unavailable / stuck / implausible / missing / none (no sensor)
    lux_entity: str | None = None
    lux_detail: str = ""
    daylight: bool | None = None      # confirmed daylight (hysteresis); None = not proven
    dusk_measured: bool = False       # measured light below the dusk level for long enough
    elevation: float | None = None
    exposed: bool = False             # sun geometry in front of the window (with hysteresis)
    exposure_text: str = ""
    strong: bool | None = None        # measured strong sunshine (hysteresis)
    fallback_daylight: bool | None = None  # sun fallback state when there is no light sensor
    vacant_since: datetime | None = None
    occupancy_known: bool = False
    deadlines: list[datetime] = field(default_factory=list)
    # 0.6.3: weather (supplementary), temperature and glare
    weather_sun: bool | None = None   # clear sky for long enough (only used without a light sensor, opt-in)
    weather_text: str = ""
    thermal: str | None = None        # cold / comfortable / warm / hot; None = unknown (or off)
    thermal_text: str = ""
    outdoor_warm: bool | None = None  # outdoor temperature at or above the "warm outside" level
    outdoor_text: str = ""
    glare: bool = False               # a glare-sensitive window whose glare conditions are met
    glare_text: str = ""
    in_sun: bool = False              # the window was in a sun condition at the last evaluation


@dataclass(slots=True)
class Flags:
    """The window's own switches and how a condition finds its target.

    ``targets(condition, phase)`` is the Global Modes resolver of the window (0.6.4); without it the plain
    ``conditions`` mapping is used (pure-logic callers)."""

    sleep_protection: bool
    privacy: bool
    solar_protection: bool
    conditions: Mapping[str, str | None]
    targets: Callable[[str, str | None], Effective] | None = None

    def lookup(self, condition: str, phase: str | None = None) -> Effective:
        if self.targets is not None:
            return self.targets(condition, phase)
        return mapping_effective(self.conditions, condition, phase)


@dataclass(slots=True)
class Decision:
    condition: str
    base: str
    preset_id: str | None
    text: str
    evidence: list[str]
    hold_release: str | None = None
    morning_start: datetime | None = None
    next_check: datetime | None = None
    opening_blocked: str | None = None
    phase: str | None = None          # what a "next phase" hold compares (Useful solar heating = its day phase)
    direct_sun: dict[str, Any] = field(default_factory=dict)
    thermal: dict[str, Any] = field(default_factory=dict)
    sun_hold: bool = False            # kept in its sun position: the weather evidence was lost
    evening_start: dict[str, Any] | None = None   # 0.6.5: an Evening that started now ({"at", "reason"})
    note: str | None = None           # 0.6.5: the schedule's explanation for the decision log
    target: Effective = NOTHING       # 0.6.4: what the condition sends, per cover, and where it comes from
    lamp_bypassed: bool = False       # the lamp is on, but the base condition is set to win over it

    @property
    def preset_name(self) -> str | None:
        return self.target.preset.name if self.target.has_target and self.target.preset else None

    def as_dict(self) -> dict[str, Any]:
        return {"condition": self.condition, "condition_word": CONDITION_WORDS.get(self.condition, self.condition),
                "mode_word": MODE_WORDS.get(self.condition, CONDITION_WORDS.get(self.condition, self.condition)),
                "base": self.base, "base_word": CONDITION_WORDS.get(self.base, self.base), "preset_id": self.preset_id,
                "preset_name": self.preset_name, "target": self.target.as_dict(),
                "source": self.target.source, "source_word": self.target.source_word,
                "text": self.text, "evidence": self.evidence, "hold_release": self.hold_release,
                "next_check": self.next_check.isoformat(timespec="seconds") if self.next_check else None,
                "opening_blocked": self.opening_blocked, "phase": self.phase, "direct_sun": self.direct_sun,
                "thermal": self.thermal, "lamp_bypassed": self.lamp_bypassed, "note": self.note}


def precedence(values: Mapping[str, Any]) -> list[dict[str, str]]:
    """The window's precedence, highest first, with the configured exceptions (shown in diagnostics)."""
    manual_bypass = set(values["manual_bypass"])
    lamp_bypass = set(values["lamp_bypass"])
    rows = [{"key": "safety", "word": "Safety: unavailable covers, competing automations, running movement, "
             "movement limits"}]
    thermal = values["thermal_enabled"]
    sun_family = (["heat"] if thermal else []) + ["sun"] + (["glare"] if values["glare_sensitive"] else [])
    words = {**CONDITION_WORDS, "heat": "Excess solar heat (room above its maximum)",
             "sun": "Direct sunlight (room warm)" if thermal else CONDITION_WORDS["sun"],
             "solar_heating": "Useful solar heating (cold room: keeps the day preset)"}
    rest = ["sleep", "night", "evening_privacy", *sun_family]

    def above(c: str) -> bool:
        return c in lamp_bypass or (c in SUN_FALLBACK and "sun" in lamp_bypass)

    above_lamp = [c for c in rest if above(c)]
    below_lamp = [c for c in rest if not above(c)]
    if thermal and values["heat_gain"] == "welcome":
        below_lamp.append("solar_heating")
    rows.append({"key": "manual_override", "word": "Manual hold" + (
        f" (ended by {', '.join(CONDITION_WORDS[c] for c in PROTECTIVE if c in manual_bypass)} when it starts later)"
        if manual_bypass else "")})
    rows += [{"key": c, "word": words[c]} for c in above_lamp]
    rows.append({"key": "lamp", "word": CONDITION_WORDS["lamp"]})
    rows += [{"key": c, "word": words[c]} for c in below_lamp]
    rows += [{"key": "morning", "word": "Morning"}, {"key": "day", "word": "Normal daytime"},
             {"key": "waiting", "word": "Waiting for daylight (keeps the position)"}]
    return rows


def _fmt(when: datetime | None) -> str:
    return when.strftime("%H:%M") if when else "—"


def _lux_text(ev: Evidence) -> str:
    if ev.lux_status == "none":
        return "no outdoor light sensor"
    if ev.lux_status == "unavailable":
        return f"the outdoor light sensor {ev.lux_entity} is unavailable"
    if ev.lux_status == "missing":
        return f"the outdoor light sensor {ev.lux_entity} does not exist"
    if ev.lux_status == "stale":
        return f"the outdoor light sensor {ev.lux_entity} has not reported for too long"
    if ev.lux_status == "stuck":
        return f"the outdoor illuminance sensor {ev.lux_entity} appears stuck ({ev.lux_detail})"
    if ev.lux_status == "implausible":
        return f"the outdoor illuminance sensor {ev.lux_entity} reads an implausible value ({ev.lux_detail})"
    return f"outdoor light is {ev.lux:.0f} lx"


def direct_sun(flags: Flags, values: Mapping[str, Any], ev: Evidence) -> dict[str, Any]:
    """Is there direct sunshine on this window? Geometry, measurement and (opt-in) weather, kept apart.

    ``status``: ``not_exposed`` (the sun is not in front), ``confirmed`` (measured strong light),
    ``likely`` (clear sky from the weather, only without a light sensor and only when allowed),
    ``not_detected`` (the sun is in front, but the light is ordinary: cloudy daylight) or
    ``uncertain`` (the sun is in front, but no trustworthy evidence)."""
    if not ev.exposed:
        return {"status": "not_exposed", "text": ev.exposure_text or "The sun is not in front of this window"}
    if ev.lux_status == "ok":
        if ev.strong is True:
            return {"status": "confirmed", "text": f"Direct sunlight detected ({ev.lux:.0f} lx)"}
        return {"status": "not_detected",
                "text": f"The sun is in front of the window, but outdoor light {ev.lux:.0f} lx is not strong "
                        f"sunshine (from {values['sun_lux']} lx for {values['sun_stable']} min): diffuse daylight"}
    if ev.lux_status == "none" and values["sun_evidence"] == "weather":
        if ev.weather_sun is True:
            return {"status": "likely", "text": f"Direct sunshine likely: {ev.weather_text} (no light sensor)"}
        if ev.weather_sun is False:
            return {"status": "not_detected", "text": f"The sun is in front of the window, but {ev.weather_text}"}
        return {"status": "uncertain", "text": "The sun is in front of the window; the weather cannot confirm "
                                               f"sunshine ({ev.weather_text or 'no weather'})"}
    if ev.lux_status == "none":
        return {"status": "uncertain", "text": "The sun is in front of the window, but there is no outdoor light "
                                               "sensor to confirm direct sunshine"}
    return {"status": "uncertain", "text": f"Direct sunshine cannot be confirmed: {_lux_text(ev)}"}


def thermal_view(values: Mapping[str, Any], ev: Evidence) -> dict[str, Any]:
    if not values["thermal_enabled"]:
        return {"state": "off", "text": "Temperature-aware sun is off"}
    if ev.thermal is None:
        return {"state": "unknown", "text": (ev.thermal_text or "Room temperature unknown")
                + ": temperature-aware sun is not used, normal sun protection applies"}
    return {"state": ev.thermal, "text": ev.thermal_text}


def _solar(values: Mapping[str, Any], ev: Evidence, direct: dict[str, Any], day_cond: str) -> tuple[str, str]:
    """The sun condition and its explanation when direct sunshine is on the window."""
    head = direct["text"]
    thermal = values["thermal_enabled"] and ev.thermal is not None
    if thermal and ev.thermal == "hot":
        return "heat", f"{head}. {ev.thermal_text}"
    if thermal and ev.thermal == "warm":
        return "sun", f"{head}. {ev.thermal_text}"
    welcome = (thermal and ev.thermal == "cold" and values["heat_gain"] == "welcome" and ev.outdoor_warm is not True)
    if ev.glare:
        tail = ". Solar heating would be welcome, but glare protection wins" if welcome else ""
        return "glare", f"{head}. Glare-sensitive window{(': ' + ev.glare_text) if ev.glare_text else ''}{tail}"
    if welcome:
        return "solar_heating", (f"{head}. {ev.thermal_text}. Solar heating is beneficial; keeping the "
                                 f"{CONDITION_WORDS[day_cond]} preset")
    if thermal:
        why = ev.thermal_text
        if ev.thermal == "cold" and ev.outdoor_warm:
            why += f", but it is warm outside ({ev.outdoor_text[:1].lower()}{ev.outdoor_text[1:]}): solar heating is " \
                "not needed"
        elif ev.thermal == "cold":
            why += " (solar heat gain is not used for warmth)"
        return "sun", f"{head}. {why}"
    if values["thermal_enabled"]:
        return "sun", f"{head}. {thermal_view(values, ev)['text']}"
    return "sun", head


def _hold_release(hold: Mapping[str, Any], base: str, phase: str, values: Mapping[str, Any], ev: Evidence,
                  active_since: Mapping[str, datetime]) -> tuple[str | None, datetime | None]:
    """Why a hold ends now (or None), and when it would end by time."""
    created: datetime = hold["created"]
    source = hold.get("source")
    for cond in values["manual_bypass"]:
        if source != "sleep_end" and base == cond and active_since.get(cond, ev.now) > created:
            return f"{CONDITION_WORDS[cond]} started and is set to end manual holds", None
    if source == "sleep_end":
        return ("Sleep started again", None) if base == "sleep" else (None, None)
    policy = hold.get("policy")
    if policy == "next_phase" or (policy == "vacant" and not ev.occupancy_known):
        if base not in PHASE_EXEMPT and phase != hold.get("phase"):
            return f"the next automatic phase started ({CONDITION_WORDS.get(base, base)})", None
        return None, None
    if policy == "duration":
        until = created + timedelta(minutes=values["manual_minutes"])
        return (f"{values['manual_minutes']} minutes have passed", None) if ev.now >= until else (None, until)
    if policy == "vacant":
        if ev.vacant_since is None:
            return None, None
        until = max(ev.vacant_since, created) + timedelta(minutes=values["vacant_minutes"])
        if ev.now >= until:
            return f"the room has been empty for {values['vacant_minutes']} minutes", None
        return None, until
    return None, None


def decide(flags: Flags, values: Mapping[str, Any], ev: Evidence, *, hold: Mapping[str, Any] | None,
           active_since: Mapping[str, datetime], morning_since: datetime | None,
           evening_since: Mapping[str, Any] | None = None) -> Decision:
    """The condition, preset and explanation for one window now."""
    now = ev.now
    notes: list[str] = []
    deadlines = list(ev.deadlines)
    night, night_change, _night_end = night_now(now, values)
    if night_change:
        deadlines.append(night_change)

    # ---- evidence sentences (shown with every decision)
    if ev.sleep is not None:
        notes.append({"on": "Sleep is on", "off": "Sleep is off",
                      "unknown": f"The sleep source {ev.sleep_entity or '(none chosen)'} is unavailable"}[ev.sleep])
    notes.append(_lux_text(ev).capitalize())
    if ev.elevation is not None:
        notes.append(f"Sun elevation {ev.elevation:.1f}°")
    if flags.solar_protection:
        notes.append(ev.exposure_text)
        if ev.weather_text:
            notes.append(f"Weather: {ev.weather_text}")
        if values["thermal_enabled"]:
            notes.append(thermal_view(values, ev)["text"])
            if ev.outdoor_text:
                notes.append(ev.outdoor_text)

    # ---- base condition
    base: str
    text: str
    opening_blocked = None
    morning_start = None
    day_cond = "day"
    sun_hold = False
    evening = now.hour >= 12
    note = None
    evening_start = None
    sleep_on = flags.sleep_protection and ev.sleep == "on"
    eve = schedule.EveningState(False)
    if not sleep_on and not night and flags.privacy:
        eve = schedule.evening(now, values, ev, evening_since)
        if eve.deadline:
            deadlines.append(eve.deadline)
    if sleep_on:
        base, text = "sleep", "Sleep mode is on"
        if not night and not evening and now >= schedule.morning_from(now, values):
            note = "Morning is eligible, but Sleep Mode is on. Holding current positions."
    elif night:
        base, text = "night", f"Night protection active until {_fmt(night_change)}"
    elif eve.active:
        base, text = "evening_privacy", eve.text
        evening_start = eve.start
        if eve.start:
            note = f"Evening started because {eve.start['reason']}."
    else:
        if eve.waiting:
            notes.append(eve.waiting)
        if ev.lux_status != "none":
            daylight_ok = ev.lux_status == "ok" and ev.daylight is True and ev.elevation is not None \
                and ev.elevation >= values["daylight_elevation"]
        else:
            daylight_ok = values["fallback"] == "sun" and ev.fallback_daylight is True
        woke_in_dark = (flags.sleep_protection and ev.sleep == "off" and not values["sleep_require_daylight"]
                        and ev.sleep_ended is not None and ev.sleep_ended.date() == now.date())
        if flags.sleep_protection and ev.sleep == "unknown":
            opening_blocked = f"the sleep source {ev.sleep_entity or '(none chosen)'} is unavailable"
        elif not daylight_ok and not woke_in_dark:
            if ev.lux_status == "ok" and ev.daylight is not True:
                opening_blocked = (f"outdoor light is {ev.lux:.0f} lx (daylight needs {values['daylight_lux']} lx "
                                   f"for {values['daylight_stable']} min)")
            elif ev.lux_status == "ok":
                opening_blocked = (f"the sun is at {ev.elevation:.1f}°, below {values['daylight_elevation']}°"
                                   if ev.elevation is not None else "the sun position is unavailable")
            elif ev.lux_status in BAD_LUX:
                opening_blocked = _lux_text(ev)
            elif values["fallback"] == "sun":
                opening_blocked = (f"the sun is at {ev.elevation:.1f}° (the fallback opens from "
                                   f"{values['fallback_elevation']}°)" if ev.elevation is not None
                                   else "the sun position is unavailable")
            else:
                opening_blocked = "there is no outdoor light sensor and no fallback is allowed"
        if opening_blocked:
            base = "waiting"
            if not evening and values["morning_enabled"] and now >= schedule.morning_from(now, values):
                note = ("Morning is eligible, but daylight has not been confirmed. Holding current positions."
                        if not (flags.sleep_protection and ev.sleep == "unknown") else
                        "Morning is eligible, but the sleep source is unavailable. Holding current positions.")
            woke = flags.sleep_protection and ev.sleep == "off" and ev.sleep_ended is not None \
                and ev.sleep_ended.date() == now.date()
            sun_note = ("Sun protection not activated because direct sunshine cannot be confirmed. "
                        if flags.solar_protection and ev.exposed and ev.lux_status in BAD_LUX else "")
            if ev.lux_status in ("stuck", "implausible") and opening_blocked == _lux_text(ev):
                text = f"{opening_blocked[0].upper()}{opening_blocked[1:]}. {sun_note}The covers keep their position"
            elif evening:
                text = f"No daylight: the covers keep their position ({opening_blocked})"
                if sun_note:
                    text += f". {sun_note.strip()}"
            else:
                lead = f"Sleep mode ended, but {opening_blocked}" if woke else \
                    opening_blocked[0].upper() + opening_blocked[1:]
                tail = "Morning preset not yet applied" if flags.lookup("morning").has_target else \
                    "The covers keep their position"
                text = f"Waiting for daylight. {lead}. {tail}"
        else:
            # Morning is a phase of its own when anything is set for it (a preset, or an explicit No action
            # that keeps the covers during the morning); with nothing at all the day starts directly (0.6.2).
            morning_used = values["morning_enabled"] and flags.lookup("morning").state != "nothing" and not (
                flags.sleep_protection and values["sleep_end"] == "resume")
            since = morning_since if morning_since is not None and morning_since.date() == now.date() else None
            # A morning starts before noon, not before the day's Morning time (0.6.5), and only once: a finished
            # Morning is never started again that day.
            if since is None and morning_used and not evening and now >= schedule.morning_from(now, values):
                since = morning_start = now
            in_morning = False
            if morning_used and since is not None:
                end = schedule.morning_end(since, now, values)
                if now < end:
                    in_morning = True
                    deadlines.append(end)
            day_cond = "morning" if in_morning else "day"
            sun_now = direct_sun(flags, values, ev) if flags.solar_protection else {}
            if sun_now.get("status") in ("confirmed", "likely"):
                base, text = _solar(values, ev, sun_now, day_cond)
            elif sun_now.get("status") == "uncertain" and ev.in_sun and ev.lux_status == "none":
                # Shading on weather evidence that is now missing: uncertain evidence never opens.
                base, sun_hold = "waiting", True
                opening_blocked = "direct sunshine can no longer be confirmed"
                text = f"{sun_now['text']}. The covers keep their sun position"
            elif in_morning:
                base = "morning"
                text = ("Morning after sleep" if woke_in_dark and not daylight_ok else "Morning: daylight confirmed")
                if daylight_ok and ev.lux_status == "ok":
                    text += f" ({ev.lux:.0f} lx)"
            else:
                base = "day"
                text = "Daylight" + (f" ({ev.lux:.0f} lx)" if ev.lux_status == "ok" else
                                     f" (sun at {ev.elevation:.1f}°, fallback)" if ev.elevation is not None else "")

    phase = (day_cond if base == "solar_heating" else base) if base not in PHASE_EXEMPT else base
    extras = {"phase": phase, "direct_sun": direct_sun(flags, values, ev) if flags.solar_protection else
              {"status": "off", "text": "Sun protection is off for this window"},
              "thermal": thermal_view(values, ev), "sun_hold": sun_hold, "evening_start": evening_start,
              "note": note}

    # ---- manual hold
    hold_release = None
    if hold is not None:
        hold_release, hold_until = _hold_release(hold, base, phase, values, ev, active_since)
        if hold_until:
            deadlines.append(hold_until)
        if hold_release is None:
            src = {"preset": "a preset was applied by hand", "sleep_end": "sleep ended and keeps its position"}.get(
                hold.get("source"), "a cover was moved by hand")
            until = {"next_phase": "until the next automatic phase", "duration": f"until {_fmt(hold_until)}",
                     "vacant": "until the room has been empty for a while", "until_released": "until you resume"}
            how = until.get(hold.get("policy"), "") if hold.get("source") != "sleep_end" else "until you resume"
            if hold.get("policy") == "vacant" and not ev.occupancy_known:
                how = "until the next automatic phase (this room has no occupancy)"
            return Decision("manual_override", base, None,
                            f"Manual override: {src}; kept {how}. Automatic would use "
                            f"{CONDITION_WORDS.get(base, base)}.",
                            notes, None, morning_start, _next(deadlines, now), opening_blocked, **extras)

    # ---- window lamp
    condition = base
    target = _target(flags, base, phase)
    lamp_bypassed = False
    if ev.lamp in ("on", "releasing") and flags.lookup("lamp").has_target:
        lamp_name = CONDITION_WORDS.get(base, base)
        if base in values["lamp_bypass"] or (base in SUN_FALLBACK and "sun" in values["lamp_bypass"]):
            text = f"{text}. Lamp override bypassed: {lamp_name} is set to win over the lamp"
            lamp_bypassed = True
        else:
            condition, target = "lamp", flags.lookup("lamp")
            text = ("Lamp override active" if ev.lamp == "on" else
                    f"Lamp off: holding the lamp preset until {_fmt(ev.lamp_release_at)}")
            bypass = [CONDITION_WORDS[c] for c in values["lamp_bypass"]]
            if bypass:
                text += f". {', '.join(bypass)} {'is' if len(bypass) == 1 else 'are'} set to win over the lamp"
            if ev.lamp_release_at:
                deadlines.append(ev.lamp_release_at)
    if condition != "waiting" and not target.has_target:
        what = CONDITION_WORDS.get(condition, condition)
        if condition in SUN_FALLBACK:
            what += " (nor for Direct sunlight)"
        elif condition == "solar_heating":
            what = CONDITION_WORDS.get(phase, phase)
        if target.state == "no_action":
            text += f". {what} is set to No action ({target.source_word}): the covers are kept as they are"
        else:
            text += f". No preset is set for {what}: the covers are kept as they are"
    return Decision(condition, base, target.preset_id, text, notes, hold_release, morning_start, _next(deadlines, now),
                    opening_blocked, **extras, target=target, lamp_bypassed=lamp_bypassed)


def _target(flags: Flags, condition: str, phase: str | None) -> Effective:
    if condition == "waiting":
        return NOTHING
    return flags.lookup(condition, phase)


def preset_for(flags: Flags, condition: str, phase: str | None) -> str | None:
    """The preset a condition uses: its own mapping; glare and excess heat fall back to Direct sunlight;
    useful solar heating keeps the Morning / Normal daytime preset."""
    return _target(flags, condition, phase).preset_id


def _next(deadlines: list[datetime], now: datetime) -> datetime | None:
    future = [d for d in deadlines if d is not None and d > now]
    return min(future) if future else None
