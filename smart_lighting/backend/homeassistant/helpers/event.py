"""Timers and state change tracking (``homeassistant.helpers.event`` subset), with Home Assistant's semantics.

A timer fires at its point in time on the event loop; when the clock has not
reached it yet (clock adjustments), it is scheduled again, as in Home Assistant.
The action receives the scheduled point in time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from ..const import EVENT_STATE_CHANGED
from ..core import CALLBACK_TYPE, Event, HomeAssistant, _run
from ..util import dt as dt_util

_TRACK = "_host_track_state_change"


def time_tracker_utcnow():  # noqa: ANN201 - the clock timers check against (as in Home Assistant; tests patch it)
    return dt_util.utcnow()


class _Tracker:
    """One ``state_changed`` listener for every tracked entity (dispatch in registration order)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.by_entity: dict[str, list[Callable[[Event], Any]]] = {}
        self._unsub: CALLBACK_TYPE | None = hass.bus.async_listen(EVENT_STATE_CHANGED, self._dispatch)

    def _dispatch(self, event: Event) -> None:
        for action in list(self.by_entity.get(event.data.get("entity_id"), ())):
            _run(self.hass, action, event)


def async_track_state_change_event(hass: HomeAssistant, entity_ids: str | Iterable[str],
                                   action: Callable[[Event], Any], job_type: Any = None) -> CALLBACK_TYPE:
    ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids)
    ids = list(dict.fromkeys(e.lower() for e in ids))
    if not ids:
        return lambda: None
    tracker: _Tracker = hass.data.get(_TRACK) or hass.data.setdefault(_TRACK, _Tracker(hass))
    for entity_id in ids:
        tracker.by_entity.setdefault(entity_id, []).append(action)

    def remove() -> None:
        for entity_id in ids:
            actions = tracker.by_entity.get(entity_id)
            if actions and action in actions:
                actions.remove(action)
                if not actions:
                    del tracker.by_entity[entity_id]

    return remove


def async_track_point_in_utc_time(hass: HomeAssistant, action: Callable[[datetime], Any],
                                  point_in_time: datetime) -> CALLBACK_TYPE:
    point = dt_util.as_utc(point_in_time)
    handle: Any = None
    cancelled = False

    def run() -> None:
        nonlocal handle
        if cancelled:
            return
        delay = (point - time_tracker_utcnow()).total_seconds()
        if delay > 0:
            handle = hass.loop.call_at(hass.loop.time() + delay, run)
            return
        handle = None
        _run(hass, action, point)

    delay = max((point - dt_util.utcnow()).total_seconds(), 0)
    handle = hass.loop.call_at(hass.loop.time() + delay, run)

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True
        if handle is not None:
            handle.cancel()

    return cancel


def async_track_point_in_time(hass: HomeAssistant, action: Callable[[datetime], Any],
                              point_in_time: datetime) -> CALLBACK_TYPE:
    return async_track_point_in_utc_time(hass, action, point_in_time)


def async_call_later(hass: HomeAssistant, delay: float | timedelta, action: Callable[[datetime], Any]) -> CALLBACK_TYPE:
    seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
    return async_track_point_in_utc_time(hass, action, dt_util.utcnow() + timedelta(seconds=seconds))


def async_track_time_interval(hass: HomeAssistant, action: Callable[[datetime], Any],
                              interval: timedelta, **_: Any) -> CALLBACK_TYPE:
    cancel: CALLBACK_TYPE | None = None
    stopped = False

    def fire(now: datetime) -> None:
        nonlocal cancel
        if stopped:
            return
        cancel = async_track_point_in_utc_time(hass, fire, now + interval)
        _run(hass, action, now)

    cancel = async_track_point_in_utc_time(hass, fire, dt_util.utcnow() + interval)

    def remove() -> None:
        nonlocal stopped
        stopped = True
        if cancel is not None:
            cancel()

    return remove
