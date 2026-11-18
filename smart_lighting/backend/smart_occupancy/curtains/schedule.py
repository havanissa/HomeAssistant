"""Global Curtain Schedule (0.6.5, the user's Phase 4.6.4). Pure logic, no Home Assistant calls.

The household day is Morning → Daytime → Evening → Night. This module is the
one place that works out when each phase begins; ``logic.decide`` (each
window) and the household view (``auto.py``) both call it, with the settings
resolved for that window (window → room → all windows, ``rules.py``).

A phase only makes a mode *eligible*. It never bypasses protections: Sleep,
Night, a manual hold, missing daylight evidence and every safety limit are
still decided by ``logic.py`` / ``auto.py``. In particular the Morning time
never opens a window in the dark or while someone sleeps.

Days and evenings, explicitly:

* A calendar day is a **free day** when its weekday is in ``free_days``,
  otherwise a **normal day**. A day's Morning, Daytime and Evening times
  follow its own profile.
* **Night** belongs to the *evening* it starts on. Its start follows the
  **late-evening** schedule on late evenings: by default the evenings before a
  free day (Fri and Sat with free Sat/Sun, exactly as 0.6.2-0.6.4), or the
  evenings chosen in ``late_days``. A start before 12:00 lies after midnight
  but still belongs to the evening before ("01:00" on Friday's row = Saturday
  01:00).
* Night ends at the **Morning time of the next day** (its own profile), which
  is also that day's earliest Morning.
* ``day_overrides`` sets single times for single weekdays: ``morning``,
  ``day``, ``evening`` for that day, ``night`` for the Night that starts on
  that weekday's evening.

Times are stored as 24-hour ``HH:mm`` local wall-clock strings and turned into
aware datetimes in Home Assistant's time zone only when used, so daylight
saving changes and restarts never shift them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

PHASES = ("morning", "day", "evening", "night")
PHASE_WORDS = {"morning": "Morning", "day": "Daytime", "evening": "Evening", "night": "Night"}
PHASE_MODES = {"morning": "morning", "day": "day", "evening": "evening_privacy", "night": "night"}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SUNSET_ELEVATION = -0.833       # the sun's upper edge at the horizon (Home Assistant's sunset)


def at(day: date, hhmm: str, tz) -> datetime:
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)


def hm(when: datetime | None) -> str:
    return when.strftime("%H:%M") if when else "—"


# ----------------------------------------------------------------- the profiles
def _plan(values: Mapping[str, Any]) -> dict[int, dict[str, str]]:
    raw = values.get("day_overrides") or ()
    if isinstance(raw, Mapping):
        return {int(k): dict(v) for k, v in raw.items()}
    return {int(d): dict(t) for d, t in raw}


def override(values: Mapping[str, Any], day: date, key: str) -> str | None:
    return _plan(values).get(day.weekday(), {}).get(key)


def is_free(day: date, values: Mapping[str, Any]) -> bool:
    return day.weekday() in values["free_days"]


def profile(day: date, values: Mapping[str, Any]) -> str:
    return "free" if is_free(day, values) else "normal"


def late_evening(evening: date, values: Mapping[str, Any]) -> bool:
    if values.get("late_evenings") == "custom":
        return evening.weekday() in (values.get("late_days") or ())
    return is_free(evening + timedelta(days=1), values)


def morning_time(day: date, values: Mapping[str, Any]) -> str:
    return override(values, day, "morning") or values["night_end_free" if is_free(day, values) else "night_end_workday"]


def day_time(day: date, values: Mapping[str, Any]) -> str:
    return override(values, day, "day") or values["day_from_free" if is_free(day, values) else "day_from_workday"]


def evening_time(day: date, values: Mapping[str, Any]) -> str:
    return override(values, day, "evening") or values[
        "evening_time_free" if is_free(day, values) else "evening_time_workday"]


def night_start(evening: date, values: Mapping[str, Any]) -> tuple[str, str]:
    """The Night start time of an evening and which schedule gave it (for explanations)."""
    if (own := override(values, evening, "night")) is not None:
        return own, f"the {WEEKDAY_NAMES[evening.weekday()]} schedule"
    if late_evening(evening, values):
        return values["night_start_free"], "the late evening schedule"
    return values["night_start_workday"], "the normal evening schedule"


def night_bounds(evening: date, values: Mapping[str, Any], tz) -> tuple[datetime, datetime]:
    """The night that begins on the evening of ``evening``."""
    morning = evening + timedelta(days=1)
    start_t, _ = night_start(evening, values)
    start = at(evening, start_t, tz) if start_t >= "12:00" else at(morning, start_t, tz)
    end = at(morning, morning_time(morning, values), tz)
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


def current_night(now: datetime, values: Mapping[str, Any]) -> tuple[date, datetime, datetime] | None:
    """The evening date and bounds of the night ``now`` is in."""
    if not values["night_enabled"]:
        return None
    for evening in (now.date() - timedelta(days=1), now.date()):
        start, end = night_bounds(evening, values, now.tzinfo)
        if start <= now < end:
            return evening, start, end
    return None


def morning_from(now: datetime, values: Mapping[str, Any]) -> datetime:
    return at(now.date(), morning_time(now.date(), values), now.tzinfo)


def day_from(now: datetime, values: Mapping[str, Any]) -> datetime:
    return at(now.date(), day_time(now.date(), values), now.tzinfo)


def morning_end(since: datetime, now: datetime, values: Mapping[str, Any]) -> datetime:
    """When Daytime begins after a Morning that began at ``since``."""
    minutes = since + timedelta(minutes=values["morning_minutes"])
    if values["morning_mode"] == "until_time":
        return day_from(now, values)
    if values["morning_mode"] == "not_before":
        return max(day_from(now, values), minutes)
    return minutes


# --------------------------------------------------------------------- evening
@dataclass(slots=True)
class EveningState:
    active: bool
    text: str = ""
    start: dict[str, Any] | None = None      # a newly started (latched) Evening: {"at", "reason"}
    deadline: datetime | None = None         # the configured time, when it is still to come today
    waiting: str | None = None               # why a due Evening cannot start


def _event(values: Mapping[str, Any], ev: Any) -> tuple[bool | None, str]:
    """Is it sunset / dusk now? None = cannot be determined."""
    if values["evening_event"] == "sunset":
        if ev.elevation is None:
            return None, "the sun position is unavailable"
        return ev.elevation <= SUNSET_ELEVATION, "the sun has set"
    if ev.dusk_measured:
        return True, f"dusk was confirmed ({ev.lux:.0f} lx)" if ev.lux is not None else "dusk was confirmed"
    if ev.elevation is not None and ev.elevation <= values["dusk_elevation"]:
        return True, f"dusk was confirmed (the sun at {ev.elevation:.1f}°)"
    if ev.lux_status == "ok" or ev.elevation is not None:
        return False, ""
    return None, "neither outdoor light nor the sun position is available"


def evening(now: datetime, values: Mapping[str, Any], ev: Any, latch: Mapping[str, Any] | None) -> EveningState:
    """Is it Evening? ``latch`` is the Evening already started today ({"at": datetime, "reason": str})."""
    rule = values["evening_rule"]
    if rule == "legacy":   # 0.6.2-0.6.4: dark after 12:00, not kept (until the new schedule is adopted)
        dark = ev.dusk_measured or (ev.elevation is not None and ev.elevation <= values["dusk_elevation"])
        if not (dark and now.hour >= 12):
            return EveningState(False)
        return EveningState(True, "Dark outside in the evening" + (
            f" ({ev.lux:.0f} lx)" if ev.lux_status == "ok" else
            f" (sun at {ev.elevation:.1f}°)" if ev.elevation is not None else ""))
    if now.hour < 12:
        return EveningState(False)
    if latch and isinstance(latch.get("at"), datetime) and latch["at"].date() == now.date():
        return EveningState(True, f"Evening since {hm(latch['at'])}: {latch.get('reason', '')}")
    target = at(now.date(), evening_time(now.date(), values), now.tzinfo)
    time_ok = now >= target
    event, why = (None, "") if rule == "time" else _event(values, ev)
    word = "sunset" if values["evening_event"] == "sunset" else "dusk"
    reason = None
    waiting = None
    if rule == "time":
        reason = f"it is {hm(target)} (the configured time)" if time_ok else None
    elif rule == "event":
        if event:
            reason = why
        elif event is None:
            waiting = f"Evening waits for {word}: {why}"
    elif rule == "first":
        if event:
            reason = f"{why} before {hm(target)}" if not time_ok else why
        elif time_ok:
            reason = f"it is {hm(target)}, before {word}" if event is False else \
                f"it is {hm(target)} ({word} cannot be determined: {why})"
    elif rule == "last":
        if time_ok and event:
            reason = f"{why} after {hm(target)}"
        elif time_ok and event is None:
            reason = f"it is {hm(target)} and {word} cannot be determined ({why}): the configured time is the fallback"
        elif time_ok:
            waiting = f"Evening waits for {word} (after {hm(target)})"
    deadline = target if not time_ok and rule in ("time", "first", "last") else None
    if reason is None:
        return EveningState(False, deadline=deadline, waiting=waiting)
    return EveningState(True, f"Evening started at {hm(now)} because {reason}", {"at": now, "reason": reason})


# ------------------------------------------------------------------- the house
@dataclass(slots=True)
class HouseEvidence:
    """What the household view knows (global settings and the global light sensor)."""

    now: datetime
    elevation: float | None = None
    lux: float | None = None
    lux_status: str = "none"
    daylight: bool | None = None
    dusk_measured: bool = False
    fallback_daylight: bool | None = None
    sun_events: Mapping[str, datetime | None] = field(default_factory=dict)  # next sunset / dusk (predictions)


def daylight_ok(values: Mapping[str, Any], ev: HouseEvidence) -> bool:
    if ev.lux_status != "none":
        return ev.lux_status == "ok" and ev.daylight is True and ev.elevation is not None \
            and ev.elevation >= values["daylight_elevation"]
    return values["fallback"] == "sun" and ev.fallback_daylight is True


def household(values: Mapping[str, Any], ev: HouseEvidence, state: dict[str, Any]) -> dict[str, Any]:
    """The household phase now (a scheduling reference; every window still decides on its own).

    ``state`` keeps ``morning_since`` and ``evening`` (latches) and is updated in place."""
    now = ev.now
    night = current_night(now, values)
    if night is not None:
        evening_date, start, end = night
        _t, which = night_start(evening_date, values)
        return {"phase": "night", "since": start, "until": end,
                "reason": f"Night started according to {which}", "waiting": None}
    eve = evening(now, values, ev, state.get("evening"))
    if eve.start:
        state["evening"] = eve.start
    if eve.active:
        latch = state.get("evening") if values["evening_rule"] != "legacy" else None
        reason = f"Evening started because {latch['reason']}" if latch else eve.text
        return {"phase": "evening", "since": latch["at"] if latch else None, "until": None, "reason": reason,
                "waiting": None}
    if state.get("morning_since") is not None and state["morning_since"].date() != now.date():
        state["morning_since"] = None
    earliest = morning_from(now, values)
    light = daylight_ok(values, ev)
    since = state.get("morning_since")
    if since is None and values["morning_enabled"] and now >= earliest and now.hour < 12 and light:
        since = state["morning_since"] = now
    if since is not None:
        end = morning_end(since, now, values)
        if now < end:
            return {"phase": "morning", "since": since, "until": end,
                    "reason": f"Morning began at {hm(since)}: daylight confirmed (earliest {hm(earliest)})",
                    "waiting": None}
        return {"phase": "day", "since": end, "until": None, "reason": f"Daytime began at {hm(end)}",
                "waiting": None if light else "no confirmed daylight: windows keep their position"}
    if now < earliest:
        return {"phase": "night", "since": None, "until": earliest,
                "reason": f"Before Morning ({hm(earliest)}); Night is off", "waiting": None}
    if values["morning_enabled"] and now.hour < 12 and not light and (
            values["morning_mode"] == "duration" or now < day_from(now, values)):
        return {"phase": "morning", "since": earliest, "until": None,
                "reason": f"Morning is eligible since {hm(earliest)}",
                "waiting": "daylight has not been confirmed: holding current positions"}
    if values["morning_mode"] != "duration" and now >= day_from(now, values):
        reason = f"Daytime since {hm(day_from(now, values))}"
    elif not values["morning_enabled"]:
        reason = "Daytime (the Morning phase is off)"
    else:
        reason = "Daytime (no Morning today: it could not begin before noon)" if now.hour >= 12 else "Daytime"
    return {"phase": "day", "since": None, "until": None, "reason": reason,
            "waiting": None if light else "no confirmed daylight: windows keep their position"}


def upcoming(now: datetime, values: Mapping[str, Any], sun_events: Mapping[str, datetime | None] | None = None,
             state: Mapping[str, Any] | None = None, days: int = 2) -> list[dict[str, Any]]:
    """The next scheduled transitions (clock predictions; Morning and Evening may still wait for evidence)."""
    tz = now.tzinfo
    sun_events = sun_events or {}
    state = state or {}
    out: list[dict[str, Any]] = []
    word = "sunset" if values["evening_event"] == "sunset" else "dusk"
    for offset in range(days + 1):
        day = now.date() + timedelta(days=offset)
        prof = "free day" if is_free(day, values) else "normal day"
        morning = at(day, morning_time(day, values), tz)
        if values["morning_enabled"]:
            out.append({"phase": "morning", "at": morning, "approx": False,
                        "reason": f"Morning from {hm(morning)} ({prof}), when daylight is confirmed and nobody sleeps"})
        since = state.get("morning_since") if offset == 0 and state.get("morning_since") else morning
        if values["morning_mode"] == "duration":
            start = since + timedelta(minutes=values["morning_minutes"])
            out.append({"phase": "day", "at": start, "approx": offset > 0 or not state.get("morning_since"),
                        "reason": f"{values['morning_minutes']} min after Morning begins"})
        else:
            start = morning_end(since, since, values) if values["morning_mode"] == "not_before" else \
                at(day, day_time(day, values), tz)
            out.append({"phase": "day", "at": start, "approx": values["morning_mode"] == "not_before",
                        "reason": f"At {day_time(day, values)} ({prof})" + (
                            f", not before Morning has lasted {values['morning_minutes']} min"
                            if values["morning_mode"] == "not_before" else "")})
        rule = values["evening_rule"]
        target = at(day, evening_time(day, values), tz)
        event = sun_events.get(word)
        event = event if event is not None and event.date() == day else None
        if rule == "legacy":
            ev_at, approx, reason = event, True, "When it is dark after 12:00 (0.6.4 behaviour)"
        elif rule == "time":
            ev_at, approx, reason = target, False, f"At {hm(target)} ({prof})"
        elif rule == "event":
            ev_at, approx, reason = event, True, f"At {word}"
        elif rule == "first":
            ev_at = min(target, event) if event else target
            approx, reason = event is not None and event < target, f"At {hm(target)} or {word}, whichever comes first"
        else:
            ev_at = max(target, event) if event else target
            approx, reason = True, f"At {hm(target)} or {word}, whichever comes last"
        if ev_at is not None:
            out.append({"phase": "evening", "at": ev_at, "approx": approx, "reason": reason})
        if values["night_enabled"]:
            start, _end = night_bounds(day, values, tz)
            _t, which = night_start(day, values)
            out.append({"phase": "night", "at": start, "approx": False,
                        "reason": f"{WEEKDAY_NAMES[day.weekday()]} evening: {which}"})
    future = sorted((x for x in out if x["at"] > now), key=lambda x: x["at"])
    return [{**x, "word": PHASE_WORDS[x["phase"]], "at": x["at"].isoformat(timespec="minutes"),
             "time": hm(x["at"])} for x in future][:8]


def timeline(day: date, values: Mapping[str, Any], tz, sun_events: Mapping[str, datetime | None] | None = None
             ) -> list[dict[str, Any]]:
    """Today's phases as clock segments (minutes from midnight) for the card's daily bar. Approximate where a
    phase waits for daylight or dusk."""
    items = upcoming(datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz) - timedelta(seconds=1), values,
                     sun_events, None, days=1)
    marks = [(0, "night" if values["night_enabled"] else "day")]
    for x in items:
        t = datetime.fromisoformat(x["at"])
        if t.date() != day:
            continue
        marks.append((t.hour * 60 + t.minute, x["phase"]))
    marks.sort()
    segments = []
    for i, (start, phase) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else 1440
        if end > start:
            segments.append({"phase": phase, "word": PHASE_WORDS[phase], "from": start, "to": end})
    return segments
