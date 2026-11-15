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

Unavailable covers, competing automations, running movements, the minimum
interval and the daily budget are *safety* limits applied by the runtime
(``auto.py``) before anything is sent; they rank above everything here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .config import CONDITION_WORDS, SUN_FALLBACK

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


# ------------------------------------------------------------------ schedule
def _at(day: date, hhmm: str, tz) -> datetime:
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)


def is_free(day: date, values: Mapping[str, Any]) -> bool:
    return day.weekday() in values["free_days"]


def night_bounds(evening: date, values: Mapping[str, Any], tz) -> tuple[datetime, datetime]:
    """The night that begins on the evening of ``evening``: the times of the morning after decide
    (a night before a free day starts and ends at the free-day times)."""
    morning = evening + timedelta(days=1)
    free = is_free(morning, values)
    start_t = values["night_start_free" if free else "night_start_workday"]
    end_t = values["night_end_free" if free else "night_end_workday"]
    start = _at(evening, start_t, tz) if start_t >= "12:00" else _at(morning, start_t, tz)
    end = _at(morning, end_t, tz)
    return start, max(start, end)


def night_now(now: datetime, values: Mapping[str, Any]) -> tuple[bool, datetime | None, datetime | None]:
    """(inside the night, when that changes next, when the current or last night ended / ends)."""
    if not values["night_enabled"]:
        return False, None, None
    tz = now.tzinfo
    today = now.date()
    for evening in (today - timedelta(days=1), today):
        start, end = night_bounds(evening, values, tz)
        if start <= now < end:
            return True, end, end
    starts = [s for d in (today - timedelta(days=1), today, today + timedelta(days=1))
              if (s := night_bounds(d, values, tz)[0]) > now]
    last_end = max((e for d in (today - timedelta(days=1), today) if (e := night_bounds(d, values, tz)[1]) <= now),
                   default=None)
    return False, min(starts) if starts else None, last_end


def day_from(now: datetime, values: Mapping[str, Any]) -> datetime:
    return _at(now.date(), values["day_from_free" if is_free(now.date(), values) else "day_from_workday"],
               now.tzinfo)


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
    """The window's own switches and its condition -> preset mapping."""

    sleep_protection: bool
    privacy: bool
    solar_protection: bool
    conditions: Mapping[str, str | None]


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

    def as_dict(self) -> dict[str, Any]:
        return {"condition": self.condition, "condition_word": CONDITION_WORDS.get(self.condition, self.condition),
                "base": self.base, "base_word": CONDITION_WORDS.get(self.base, self.base), "preset_id": self.preset_id,
                "text": self.text, "evidence": self.evidence, "hold_release": self.hold_release,
                "next_check": self.next_check.isoformat(timespec="seconds") if self.next_check else None,
                "opening_blocked": self.opening_blocked, "phase": self.phase, "direct_sun": self.direct_sun,
                "thermal": self.thermal}


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
           active_since: Mapping[str, datetime], morning_since: datetime | None) -> Decision:
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
    dark = ev.dusk_measured or (ev.elevation is not None and ev.elevation <= values["dusk_elevation"])
    sleep_on = flags.sleep_protection and ev.sleep == "on"
    if sleep_on:
        base, text = "sleep", "Sleep mode is on"
    elif night:
        base, text = "night", f"Night protection active until {_fmt(night_change)}"
    elif flags.privacy and dark and evening:
        base = "evening_privacy"
        text = "Dark outside in the evening" + (f" ({ev.lux:.0f} lx)" if ev.lux_status == "ok" else
                                                f" (sun at {ev.elevation:.1f}°)" if ev.elevation is not None else "")
    else:
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
                tail = "Morning preset not yet applied" if flags.conditions.get("morning") else \
                    "The covers keep their position"
                text = f"Waiting for daylight. {lead}. {tail}"
        else:
            mapped_morning = flags.conditions.get("morning")
            morning_used = mapped_morning is not None and not (
                flags.sleep_protection and values["sleep_end"] == "resume")
            since = morning_since if morning_since is not None and morning_since.date() == now.date() else None
            if since is None and morning_used and not evening:  # a morning starts before noon
                since = morning_start = now
            in_morning = False
            if morning_used and since is not None:
                if values["morning_mode"] == "until_time":
                    end = day_from(now, values)
                else:
                    end = since + timedelta(minutes=values["morning_minutes"])
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
              "thermal": thermal_view(values, ev), "sun_hold": sun_hold}

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
    condition, preset = base, preset_for(flags, base, phase)
    if ev.lamp in ("on", "releasing") and flags.conditions.get("lamp"):
        lamp_name = CONDITION_WORDS.get(base, base)
        if base in values["lamp_bypass"] or (base in SUN_FALLBACK and "sun" in values["lamp_bypass"]):
            text = f"{text}. Lamp override bypassed: {lamp_name} is set to win over the lamp"
        else:
            condition, preset = "lamp", flags.conditions.get("lamp")
            text = ("Lamp override active" if ev.lamp == "on" else
                    f"Lamp off: holding the lamp preset until {_fmt(ev.lamp_release_at)}")
            bypass = [CONDITION_WORDS[c] for c in values["lamp_bypass"]]
            if bypass:
                text += f". {', '.join(bypass)} {'is' if len(bypass) == 1 else 'are'} set to win over the lamp"
            if ev.lamp_release_at:
                deadlines.append(ev.lamp_release_at)
    if condition != "waiting" and preset is None:
        what = CONDITION_WORDS.get(condition, condition)
        if condition in SUN_FALLBACK:
            what += " (nor for Direct sunlight)"
        elif condition == "solar_heating":
            what = CONDITION_WORDS.get(phase, phase)
        text += f". No preset is set for {what}: the covers are kept as they are"
    return Decision(condition, base, preset, text, notes, hold_release, morning_start, _next(deadlines, now),
                    opening_blocked, **extras)


def preset_for(flags: Flags, condition: str, phase: str | None) -> str | None:
    """The preset a condition uses: its own mapping; glare and excess heat fall back to Direct sunlight;
    useful solar heating keeps the Morning / Normal daytime preset."""
    if condition == "waiting":
        return None
    if condition == "solar_heating":
        return flags.conditions.get(phase or "day")
    preset = flags.conditions.get(condition)
    if preset is None and condition in SUN_FALLBACK:
        preset = flags.conditions.get("sun")
    return preset


def _next(deadlines: list[datetime], now: datetime) -> datetime | None:
    future = [d for d in deadlines if d is not None and d > now]
    return min(future) if future else None
