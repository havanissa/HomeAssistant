"""Mood lighting controller: one shared executor for every mood.

Applies the resolved mood profile of a room when:

* the effective mood changes (Main Mood, or the room's own mood override),
* the room's occupancy is newly confirmed (a new visit),
* the room is activated, or a profile entry that applies to it changes
  (only the lights whose resolved setting changed),
* a light that was offline during an application comes back.

Commands are sent only in rooms whose lighting control was explicitly
enabled (``RoomLighting.control_enabled``); every other room stays in
Observe mode and gets plans (previews) only. Nothing is applied while Home
Assistant is starting or on a restart.

Occupancy is taken from the Phase 0.2 engine (OccupancyManager); there is no
second occupancy engine. Manual changes are recognised from the Home
Assistant context of light state changes and are respected for a configurable
time. Existing automations are never changed; when an enabled automation also
appears to control a light, that light is left alone until the user
acknowledges or resolves the conflict.

Global light adjustment (0.3.2): profiles can follow the values of the
user's existing adaptive brightness / colour temperature system
(``adaptive.py``). Mood applications use the current values; when a value
changes, lights that are already on get a gradual update (coalesced,
throttled per light, only meaningful changes). A global change never
switches a light on. Lights the existing system commands itself are not
adjusted by both.

Manual (0.3.2): while Main Mood is the Manual option, no automatic lighting
command is sent at all (mood, occupancy or global adjustment), queued
applications and updates are dropped, and manual light changes are not
recorded as overrides. Occupancy, diagnostics and editing continue.

Daylight (0.3.3): occupancy switches lights on only when the room needs
artificial light (``daylight.py``, ``daylight_monitor.py``). The daylight
decision says whether an off light may be switched on; the mood profile says
what it looks like. A visit starts on the first motion of a trigger sensor
(PIR) when the room allows it, otherwise on confirmed occupancy; radar and
other presence sensors hold occupancy. An occupied room that becomes dark is
evaluated again. Nothing is switched off because of daylight.

Wall switches (0.3.5): configured switches and remotes deliver button events
(``switch_inputs.py``). Every press marks the next light changes in the room
as a person's (manual override). Switches set to Active execute their button
mappings (``switch_control.py``) through ``async_switch_lights``. That is
gated by the switch's own mode only (0.3.6): the room's automatic lighting
mode (Observing / Automatic) does not block a deliberately activated switch,
Manual mood does not pause it (a person's explicit request), and the lights it
switches get a manual override, so automatic lighting does not undo it. A
switch in Observing never sends anything.

Vacancy (0.3.8): when the occupancy engine reports the room vacant (its
vacancy timeout, from the room's presence settings, has expired), the room's
vacancy action runs once: Turn off, a vacant mood, or Leave unchanged
(``planner.build_vacancy_plan``). There is no second timer: the engine's
possible-vacancy period is the timeout, and presence returning during it
cancels the pending action (nothing is switched off). Manual overrides made
before the room went quiet end with the vacancy (as before); one made while
the room was already quiet (a person deliberately changed a light in an
empty room) is respected until it expires, and the vacancy action then runs
for that light if the room is still vacant. A light that was unavailable is
handled when it comes back. Ambient lights, Observing, Manual mode and the
other protections apply as for every mood plan. Switching a vacant room to
Automatic does not switch anything off: the action runs on a transition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import mood as mood_mod
from .adaptive import (
    DEFERRED,
    SEND,
    AdaptiveDecision,
    AdaptiveSent,
    GlobalAdjustment,
    GlobalValues,
    SourceController,
    adaptive_update,
    apply_global,
    find_source_controllers,
)
from .conflicts import AutomationInfo, Conflict, collect_automations, find_conflicts
from .daylight import GATE_CODES, GATE_NEVER, GATE_NOT_NEEDED, GATE_UNCERTAIN, DaylightGate
from .daylight_monitor import DaylightMonitor, DaylightStatus
from .decisions import DecisionLog
from .engine import RoomResult, SignalStatus
from .const import (
    ADAPTIVE_DEBOUNCE_SECONDS,
    APPLY_DEBOUNCE_SECONDS,
    COMMAND_TIMEOUT_SECONDS,
    LIGHTING_STATE_SAVE_DELAY_SECONDS,
    LIGHTING_STATE_STORE_KEY,
    LIGHTING_STATE_STORE_VERSION,
    MANUAL_PAUSED_TEXT,
    OWN_CHANGE_WINDOW_SECONDS,
    SIGNAL_LIGHTING_UPDATED,
    SWITCH_ATTRIBUTION_SECONDS,
    AutoOn,
    LightingMode,
    OccupancyState,
    UnattributedPolicy,
    UnrepresentableMode,
    VacancyAction,
)
from .const import ALL_MOODS
from .discovery import area_name
from .lighting import LightIndex, expand_group, light_capabilities
from .lighting_config import LightingConfig, LightingStore
from .lighting_decisions import LightingDecisions
from .models import IntegrationConfig
from .occupancy import friendly_name
from .planner import BLOCKED, CHANGE, Plan, PlanItem, Target, build_plan, build_vacancy_plan
from .profiles import resolve
from .supervision import assert_room_control_permitted, assert_switch_control_permitted
from .switch_control import SwitchControl
from .switch_inputs import SwitchInputs
from .switches import ButtonEvent, SwitchConfig

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (STATE_UNAVAILABLE, STATE_UNKNOWN)
_SIGNIFICANT_ATTRS = ("brightness", "color_temp_kelvin", "rgb_color", "hs_color", "xy_color", "effect", "color_mode")
_AUTOMATION_DEBOUNCE_SECONDS = 2.0
_PAUSED = "__paused__"
# Triggers that can switch lights on because someone is (or arrived) in the room.
_ACTIVATION_TRIGGERS = frozenset({"motion", "occupancy_confirmed", "became_dark"})
_TRIGGER_WORDS = {"motion": "motion", "occupancy_confirmed": "occupancy confirmed", "became_dark": "it became dark"}


@dataclass(slots=True, frozen=True)
class MoodStatus:
    """The effective global mood for lighting, and how it was determined."""

    mood: str | None
    source: str  # main | follow_main | no_mood | use_mood | main_unavailable | not_configured | manual
    main_mood: str | None
    expected: str | None
    follows: str | None  # "auto" | "manual"
    unrepresentable: str | None
    detail: str
    # Main Mood is the Manual option: automatic lighting is paused.
    manual: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "manual_mode": self.manual,
            "mood": self.mood,
            "source": self.source,
            "main_mood": self.main_mood,
            "expected_main_mood": self.expected,
            "follows": self.follows,
            "unrepresentable": self.unrepresentable,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ManagedRoom:
    """Which lights a room manages, after discovery, exclusion and group handling."""

    area_id: str
    targets: list[str] = field(default_factory=list)  # every managed light or group (incl. excluded)
    explicit: set[str] = field(default_factory=set)
    discovered: set[str] = field(default_factory=set)
    excluded: dict[str, str] = field(default_factory=dict)  # entity -> reason
    covered_by: dict[str, str] = field(default_factory=dict)  # member -> managed group
    members: dict[str, set[str]] = field(default_factory=dict)  # group -> members (known)
    opaque_groups: list[str] = field(default_factory=list)
    outside_area: list[str] = field(default_factory=list)

    @property
    def controlled(self) -> list[str]:
        """Targets that receive commands (not excluded, not covered by a group)."""
        return [t for t in self.targets if t not in self.excluded and t not in self.covered_by]


@dataclass(slots=True)
class RoomRuntime:
    visit_confirmed: bool = False
    occupancy_state: str | None = None
    occupied: bool | None = None
    last_applied_mood: str | None = None
    last_applied_at: str | None = None
    last_trigger: str | None = None
    last_result: str | None = None
    last_reason: str | None = None
    last_counts: dict[str, int] = field(default_factory=dict)
    last_errors: list[str] = field(default_factory=list)
    last_decision_at: str | None = None
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_commands: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    pending_reconnect: set[str] = field(default_factory=set)
    last_external: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_plan: Plan | None = None
    # Last command per light/group, for gradual updates (monotonic; not persisted).
    adaptive_sent: dict[str, AdaptiveSent] = field(default_factory=dict)
    last_adaptive_at: str | None = None
    last_adaptive: str | None = None
    # A visit started (first motion or confirmation) and was counted (0.3.3).
    visit_started: bool = False
    visit: int = 0
    # Why occupancy did or did not switch lights on (latest visit or darkness change).
    activation: dict[str, Any] | None = None
    last_activation_at: str | None = None
    # A temporary room mood set by a wall switch (0.3.5): {"mood", "until"}.
    temp_mood: dict[str, Any] | None = None
    # The visit whose activation decision was logged last (0.3.7; not persisted).
    decision_visit: int = -1
    # Vacancy (0.3.8): lights whose vacancy action is still due (kept by a manual override, or unavailable).
    vacancy_deferred: set[str] = field(default_factory=set)
    # The logged "vacancy timeout started" decision and when it started (monotonic; not persisted).
    vacancy_wait: tuple[str, float] | None = None
    # The last vacancy action: {"at", "action", "result", "text"}.
    last_vacancy: dict[str, Any] | None = None

    def persist(self) -> dict[str, Any]:
        return {
            "last_applied_mood": self.last_applied_mood,
            "last_applied_at": self.last_applied_at,
            "last_trigger": self.last_trigger,
            "last_result": self.last_result,
            "last_reason": self.last_reason,
            "last_counts": self.last_counts,
            "last_errors": self.last_errors,
            "last_decision_at": self.last_decision_at,
            "overrides": self.overrides,
            "last_commands": {e: [s, d] for e, (s, d) in self.last_commands.items()},
            "activation": self.activation,
            "last_activation_at": self.last_activation_at,
            "temp_mood": self.temp_mood,
            "vacancy_deferred": sorted(self.vacancy_deferred),
            "last_vacancy": self.last_vacancy,
        }

    @classmethod
    def restore(cls, data: Any) -> RoomRuntime:
        data = data if isinstance(data, dict) else {}
        commands = data.get("last_commands") if isinstance(data.get("last_commands"), dict) else {}
        activation = data.get("activation") if isinstance(data.get("activation"), dict) else None
        return cls(
            last_applied_mood=data.get("last_applied_mood"),
            last_applied_at=data.get("last_applied_at"),
            last_trigger=data.get("last_trigger"),
            last_result=data.get("last_result"),
            last_reason=data.get("last_reason"),
            last_counts=data.get("last_counts") or {},
            last_errors=list(data.get("last_errors") or []),
            last_decision_at=data.get("last_decision_at"),
            overrides={e: o for e, o in (data.get("overrides") or {}).items() if isinstance(o, dict)},
            last_commands={
                e: (v[0], dict(v[1])) for e, v in commands.items()
                if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict)
            },
            activation=activation,
            last_activation_at=data.get("last_activation_at"),
            temp_mood=data.get("temp_mood") if isinstance(data.get("temp_mood"), dict) else None,
            vacancy_deferred={e for e in data.get("vacancy_deferred") or [] if isinstance(e, str)},
            last_vacancy=data.get("last_vacancy") if isinstance(data.get("last_vacancy"), dict) else None,
            # Continue counting visits, so the next visit is never mistaken for the recorded one.
            visit=int(activation.get("visit") or 0) if activation else 0,
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _significant(old: State, new: State) -> bool:
    if old.state != new.state:
        return True
    if new.state != "on":
        return False
    return any(old.attributes.get(k) != new.attributes.get(k) for k in _SIGNIFICANT_ATTRS)


class LightingController:
    """Plans and (in activated rooms) executes mood lighting."""

    def __init__(self, hass: HomeAssistant, coordinator: SmartOccupancyCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.store = LightingStore(hass)
        self._state_store: Store[dict[str, Any]] = Store(
            hass, LIGHTING_STATE_STORE_VERSION, LIGHTING_STATE_STORE_KEY
        )
        self.runtime: dict[str, RoomRuntime] = {}
        self._index: LightIndex | None = None
        self._managed: dict[str, ManagedRoom] | None = None
        self._automations: list[AutomationInfo] | None = None
        self._conflicts: dict[str, dict[str, list[Conflict]]] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._track_unsub: CALLBACK_TYPE | None = None
        self._tracked: tuple[str, ...] = ()
        self._light_targets: dict[str, list[tuple[str, str]]] = {}  # entity -> [(area, target)]
        self._switch_rooms: dict[str, list[str]] = {}
        self._switch_seen: dict[str, tuple[float, str]] = {}  # area -> (monotonic, context id)
        self._switch_contexts: dict[str, float] = {}
        self._own_contexts: dict[str, float] = {}
        self._expected: dict[str, tuple[float, str]] = {}  # entity -> (until, service)
        self._pending: dict[str, tuple[set[str], set[str] | None]] = {}  # area -> (triggers, only)
        self._started_unsub: CALLBACK_TYPE | None = None
        self._apply_timer: CALLBACK_TYPE | None = None
        self._override_timer: CALLBACK_TYPE | None = None
        self._automation_timer: CALLBACK_TYPE | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_mood: str | None = None
        self._manual = False
        self._running = False
        self._stopped = False
        self._dirty = False
        self.adaptive = GlobalAdjustment(hass, self._on_global_change)
        self._adaptive_timers: dict[str, tuple[CALLBACK_TYPE, float]] = {}  # area -> (cancel, due)
        self._adaptive_force: dict[str, set[str]] = {}
        self._source_controllers: tuple[list[SourceController], list[AutomationInfo]] | None = None
        self._adaptive_conflicts: dict[str, dict[str, list[dict[str, Any]]]] | None = None
        self.daylight = DaylightMonitor(hass, self)
        # Contexts of commands sent for a wall switch press (their own state reports are not new overrides).
        self._switch_commands: dict[str, float] = {}
        self._restored_switches: dict[str, Any] = {}
        self.switch_control = SwitchControl(hass, self)
        self.switch_inputs = SwitchInputs(hass, self, self._on_button)
        # Decision log (0.3.7): rooms and switches, saved with this state store.
        self.decisions = DecisionLog(hass, self._on_decision)
        self.explain = LightingDecisions(self)

    # ------------------------------------------------------------ properties
    @property
    def config(self) -> IntegrationConfig:
        return self.coordinator.config

    @property
    def lighting(self) -> LightingConfig:
        return self.store.config

    @property
    def index(self) -> LightIndex:
        if self._index is None:
            self._index = LightIndex(self.hass, self.config.categories)
        return self._index

    @property
    def managed(self) -> dict[str, ManagedRoom]:
        if self._managed is None:
            self._managed = {a: self._compute_managed(a) for a in sorted(self.config.enabled_rooms)}
        return self._managed

    def managed_room(self, area_id: str) -> ManagedRoom:
        return self.managed.get(area_id) or ManagedRoom(area_id)

    def room_active(self, area_id: str) -> bool:
        """Lighting control permitted: the room is enabled and was explicitly activated."""
        return area_id in self.config.enabled_rooms and self.lighting.room(area_id).control_enabled

    def switch_active(self, area_id: str, switch_id: str) -> bool:
        """Switch actions permitted: the room is enabled and the switch is set to Active (now, not when queued)."""
        if area_id not in self.config.enabled_rooms:
            return False
        switch = self.lighting.room(area_id).switch(switch_id)
        return switch is not None and switch.controls

    @property
    def manual_mode(self) -> bool:
        """Main Mood is the Manual option: no automatic lighting command is sent."""
        return self.mood_status().manual

    def global_values(self) -> GlobalValues:
        return self.adaptive.values

    # -------------------------------------------------------------- lifecycle
    async def async_load(self) -> None:
        await self.store.async_load(self.config)
        data = await self._state_store.async_load() or {}
        rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        self.adaptive.restore(data.get("adaptive"))
        self.adaptive.async_configure(self.lighting.adaptive)
        self.daylight.restore(data.get("daylight"))
        self._restored_switches = data.get("switches") if isinstance(data.get("switches"), dict) else {}
        self.decisions.restore(data.get("decisions"))
        now = dt_util.utcnow()
        for area_id in self.config.enabled_rooms:
            rt = RoomRuntime.restore(rooms.get(area_id))
            # Drop overrides that expired while Home Assistant was not running.
            rt.overrides = {
                e: o for e, o in rt.overrides.items()
                if not o.get("until") or (dt_util.parse_datetime(o["until"]) or now) > now
            }
            self.runtime[area_id] = rt

    @callback
    def async_start(self) -> None:
        self._unsubs.append(self.store.async_add_listener(self._on_lighting_config_changed))
        for area_id in self.config.enabled_rooms:
            self._unsubs.append(
                self.coordinator.occupancy.async_add_listener(area_id, self._occupancy_listener(area_id))
            )
        self._unsubs.append(self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._on_automation_state, event_filter=self._automation_filter
        ))
        self._unsubs.append(self.hass.bus.async_listen("automation_reloaded", self._on_automations_reloaded))
        self._unsubs.append(self.hass.bus.async_listen("script_reloaded", self._on_automations_reloaded))
        self._resubscribe()
        self.adaptive.async_start()
        self.daylight.async_start()
        self.switch_inputs.async_start(self._restored_switches)
        self.switch_control.async_start()
        if self.hass.state is CoreState.running:
            self._on_started()
        else:
            self._started_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)
        self._schedule_override_expiry()

    @callback
    def _on_started(self, _event: Event | None = None) -> None:
        """Take the current mood and occupancy as baseline. Nothing is applied on start."""
        self._started_unsub = None  # a one-shot listener is removed when it fires
        if self._stopped:
            return
        self._running = True
        status = self.mood_status()
        self._last_mood = status.mood
        self._manual = status.manual
        for area_id in self.config.enabled_rooms:
            result = self.coordinator.occupancy.result(area_id)
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            if result is not None:
                rt.occupancy_state, rt.occupied = result.state.value, result.occupied
                rt.visit_confirmed = bool(result.occupied) and result.state != OccupancyState.POSSIBLE_OCCUPANCY
                # Someone already in the room at start is not a new visit.
                rt.visit_started = bool(result.occupied)
        self._automations = None
        self._conflicts = None
        _LOGGER.debug(
            "lighting_start mood=%s manual=%s active_rooms=%s", self._last_mood, self._manual,
            self.lighting.active_rooms,
        )
        self._notify()

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        self._running = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._track_unsub:
            self._track_unsub()
            self._track_unsub = None
        for attr in ("_started_unsub", "_apply_timer", "_override_timer", "_automation_timer"):
            if cancel := getattr(self, attr):
                cancel()
                setattr(self, attr, None)
        self._cancel_adaptive()
        self.adaptive.async_stop()
        self.daylight.async_stop()
        self.switch_control.async_stop()
        self.switch_inputs.async_stop()
        self.decisions.async_stop()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._pending.clear()

    async def async_flush(self) -> None:
        await self.store.async_flush()
        if self._dirty:
            await self._state_store.async_save(self._state_data())

    async def async_remove(self) -> None:
        await self.store.async_remove()
        await self._state_store.async_remove()

    # ---------------------------------------------------------- invalidation
    @callback
    def async_registry_changed(self) -> None:
        """Entity, device, area or label registry changed (debounced by the coordinator)."""
        self._index = None
        self._managed = None
        self._conflicts = None
        self._adaptive_conflicts = None
        self._resubscribe()
        self.daylight.async_config_changed()
        self.switch_inputs.async_registry_changed()
        self._notify()

    def _invalidate_managed(self) -> None:
        self._managed = None
        self._conflicts = None
        self._adaptive_conflicts = None
        self._resubscribe()
        self.daylight.resubscribe()

    # ------------------------------------------------------------ discovery
    def _compute_managed(self, area_id: str) -> ManagedRoom:
        hass = self.hass
        index = self.index
        room_cfg = self.config.room(area_id)
        room_lighting = self.lighting.room(area_id)
        exclude_label = self.lighting.exclude_label
        out = ManagedRoom(area_id)
        explicit = [e for e in room_cfg.lights if e.split(".", 1)[0] == "light"]
        discovered = index.labelled_in_area(area_id) if room_lighting.auto_discover else []
        out.explicit = set(explicit)
        out.discovered = set(discovered) - out.explicit
        out.targets = list(dict.fromkeys([*explicit, *sorted(discovered)]))
        for entity_id in out.targets:
            if entity_id in room_lighting.excluded:
                out.excluded[entity_id] = "excluded in this room"
            elif index.has_label(entity_id, exclude_label):
                out.excluded[entity_id] = "exclusion label"
            info = index.info(entity_id)
            if entity_id in out.explicit and info is not None and info.area_id not in (None, area_id):
                out.outside_area.append(entity_id)
            if members := expand_group(hass, entity_id):
                out.members[entity_id] = members
            elif index.is_opaque_group(entity_id):
                out.opaque_groups.append(entity_id)
        for group, members in out.members.items():
            if group in out.excluded:
                continue  # an excluded group's members are controlled individually
            for member in members:
                if member in out.targets and member != group and member not in out.members.get(member, set()):
                    out.covered_by.setdefault(member, group)
        return out

    def _resubscribe(self) -> None:
        """One state subscription for mood helpers, managed lights (and group members) and switches."""
        if self._stopped:
            return
        light_targets: dict[str, list[tuple[str, str]]] = {}
        switch_rooms: dict[str, list[str]] = {}
        for area_id, managed in self.managed.items():
            for target in managed.targets:
                light_targets.setdefault(target, []).append((area_id, target))
            # A change to a member of a managed group is a change to that group: a person
            # switching one spot keeps the group from being commanded over it.
            for group, members in managed.members.items():
                if group in managed.controlled:
                    for member in members:
                        if (area_id, group) not in light_targets.setdefault(member, []):
                            light_targets[member].append((area_id, group))
            for switch in self.lighting.room(area_id).manual_switches:
                switch_rooms.setdefault(switch, []).append(area_id)
        self._light_targets, self._switch_rooms = light_targets, switch_rooms
        tracked = tuple(sorted({*self.config.mood.entity_ids, *light_targets, *switch_rooms}))
        if tracked == self._tracked and self._track_unsub is not None:
            return
        if self._track_unsub:
            self._track_unsub()
            self._track_unsub = None
        self._tracked = tracked
        if tracked:
            self._track_unsub = async_track_state_change_event(self.hass, list(tracked), self._on_state_event)

    # --------------------------------------------------------------- moods
    def mood_status(self) -> MoodStatus:
        obs = mood_mod.observe(self.hass, self.config.mood)
        main = obs.main_mood
        expected = obs.expected_main_mood
        options = {o.casefold() for o in obs.main_mood_options}
        unrep = expected if expected and options and expected.casefold() not in options else None
        base = {"main_mood": main, "expected": expected, "follows": obs.source, "unrepresentable": unrep}
        if not self.config.mood.main_mood_entity:
            return MoodStatus(None, "not_configured", detail="Main Mood is not configured", **base)
        if main is None:
            return MoodStatus(None, "main_unavailable", detail="Main Mood is unavailable; lighting is not changed", **base)
        if self.lighting.is_manual(main):
            # Manual is an operating mode, not a mood: it takes precedence over everything.
            return MoodStatus(None, "manual", detail=MANUAL_PAUSED_TEXT, manual=True, **base)
        if unrep:
            handling = self.lighting.handling_for(unrep)
            who = "Auto Mood" if obs.source == "auto" else "Manual Mood"
            if handling.mode == UnrepresentableMode.NO_MOOD:
                return MoodStatus(None, "no_mood", detail=(
                    f"{who} is '{unrep}', which Main Mood cannot represent; it is handled as 'no mood', "
                    "so lighting is not changed"), **base)
            if handling.mode == UnrepresentableMode.USE_MOOD and handling.mood:
                return MoodStatus(handling.mood, "use_mood", detail=(
                    f"{who} is '{unrep}', which Main Mood cannot represent; lighting uses '{handling.mood}'"), **base)
            return MoodStatus(main, "follow_main", detail=(
                f"{who} is '{unrep}', which Main Mood cannot represent; lighting keeps following Main Mood "
                f"('{main}')"), **base)
        return MoodStatus(main, "main", detail=f"Main Mood is '{main}'", **base)

    def room_mood(self, area_id: str, status: MoodStatus | None = None) -> tuple[str | None, str]:
        status = status or self.mood_status()
        if status.manual:
            return None, "manual"  # Manual beats a room's own mood too
        override = self.lighting.room(area_id).mood_override
        if override:
            return override, "room_override"
        return status.mood, "global"

    def discovered_moods(self) -> list[dict[str, Any]]:
        """Every mood offered by the three selectors, plus moods that only have profiles."""
        hass, mood_cfg = self.hass, self.config.mood
        auto_option = mood_cfg.auto_option.casefold()

        def options(entity_id: str | None) -> list[str]:
            state = hass.states.get(entity_id) if entity_id else None
            return [str(o) for o in (state.attributes.get("options") or [])] if state else []

        main, auto, manual = (options(mood_cfg.main_mood_entity), options(mood_cfg.auto_mood_entity),
                              options(mood_cfg.manual_mood_entity))
        names: dict[str, str] = {}
        for name in (*main, *auto, *manual, *self.lighting.global_profiles, *self.lighting.parents):
            # Neither Auto nor Manual is a mood; "*" holds the entries for every mood.
            if name.casefold() != auto_option and not self.lighting.is_manual(name) and name != ALL_MOODS:
                names.setdefault(name.casefold(), name)
        if self.lighting.fallback_mood:
            names.setdefault(self.lighting.fallback_mood.casefold(), self.lighting.fallback_mood)

        def has(lst: list[str], name: str) -> bool:
            return name.casefold() in {o.casefold() for o in lst}

        return [
            {
                "name": name,
                "in_main": has(main, name),
                "in_auto": has(auto, name),
                "in_manual": has(manual, name),
                "parent": self.lighting.parent_of(name),
                "has_profile": bool(next((t for m, t in self.lighting.global_profiles.items()
                                          if m.casefold() == key), None)),
                "handling": self.lighting.handling_for(name).as_dict() if not has(main, name) else None,
            }
            for key, name in names.items()
        ]

    # ------------------------------------------------------------ conflicts
    @callback
    def _automation_filter(self, event_data: EventStateChangedData) -> bool:
        if not event_data["entity_id"].startswith("automation."):
            return False
        old, new = event_data["old_state"], event_data["new_state"]
        return (old.state if old else None) != (new.state if new else None)

    @callback
    def _on_automation_state(self, _event: Event) -> None:
        self._schedule_automation_refresh()

    @callback
    def _on_automations_reloaded(self, _event: Event) -> None:
        self._schedule_automation_refresh()

    @callback
    def _schedule_automation_refresh(self) -> None:
        if self._automation_timer or self._stopped:
            return

        @callback
        def fire(_now: datetime) -> None:
            self._automation_timer = None
            self._automations = None
            self._conflicts = None
            self._source_controllers = None
            self._adaptive_conflicts = None
            self.coordinator.async_schedule_refresh()
            self._notify()

        self._automation_timer = async_call_later(self.hass, _AUTOMATION_DEBOUNCE_SECONDS, fire)

    def conflicts(self, area_id: str) -> dict[str, list[Conflict]]:
        """Automations that appear to control each managed light of a room (cached)."""
        if self._conflicts is None:
            if self._automations is None:
                self._automations = collect_automations(self.hass)
            all_targets = {t for m in self.managed.values() for t in m.targets}
            containing = self.index.groups_containing(all_targets) if self._automations else {}
            self._conflicts = {
                a: find_conflicts(self.hass, self.index, m.targets, self._automations, containing)
                for a, m in self.managed.items()
            }
        return self._conflicts.get(area_id, {})

    def source_controllers(self) -> list[SourceController]:
        """Parts of the existing adjustment system that command lights themselves (cached)."""
        return self._source_info()[0]

    def _source_info(self) -> tuple[list[SourceController], list[AutomationInfo]]:
        if self._source_controllers is None:
            if self._automations is None:
                self._automations = collect_automations(self.hass)
            self._source_controllers = find_source_controllers(self.hass, self.lighting.adaptive, self._automations)
        return self._source_controllers

    def adaptive_conflicts(self, area_id: str) -> dict[str, list[dict[str, Any]]]:
        """Managed lights that an active part of the existing adjustment system also adjusts."""
        if self._adaptive_conflicts is None:
            controllers, automations = self._source_info()
            out: dict[str, dict[str, list[dict[str, Any]]]] = {}
            switches = [c for c in controllers if c.kind == "adaptive_lighting" and c.active]
            active_autos = [a for a in automations if a.enabled]
            for area, managed in self.managed.items():
                room: dict[str, list[dict[str, Any]]] = {}
                if active_autos:
                    for light, found in find_conflicts(self.hass, self.index, managed.targets, active_autos).items():
                        room.setdefault(light, []).extend(
                            {"entity_id": c.automation, "name": c.name, "kind": "automation", "detail": c.detail}
                            for c in found)
                for switch in switches:
                    for light in managed.targets:
                        related = {light, *expand_group(self.hass, light)}
                        if related & switch.lights:
                            room.setdefault(light, []).append(
                                {"entity_id": switch.entity_id, "name": switch.name, "kind": "adaptive_lighting",
                                 "detail": "Adaptive Lighting switch is on and adjusts this light"})
                out[area] = room
            self._adaptive_conflicts = out
        return self._adaptive_conflicts.get(area_id, {})

    def blocking_conflicts(self, area_id: str, entity_id: str) -> list[Conflict]:
        acknowledged = set(self.lighting.room(area_id).acknowledged)
        return [c for c in self.conflicts(area_id).get(entity_id, [])
                if c.enabled and c.automation not in acknowledged]

    # --------------------------------------------------------------- plans
    def _targets(self, area_id: str) -> list[Target]:
        hass = self.hass
        managed = self.managed_room(area_id)
        rt = self.runtime.get(area_id) or RoomRuntime()
        acknowledged = set(self.lighting.room(area_id).acknowledged)
        out: list[Target] = []
        for entity_id in managed.targets:
            state = hass.states.get(entity_id)
            category = self.index.category_of(entity_id)
            cat = self.config.category(category) if category else None
            conflicts = self.conflicts(area_id).get(entity_id, [])
            out.append(Target(
                entity_id=entity_id,
                name=friendly_name(hass, entity_id),
                category=category,
                category_name=cat.name if cat else None,
                state=state.state if state else None,
                attributes=dict(state.attributes) if state else {},
                capabilities=light_capabilities(hass, entity_id),
                is_group=entity_id in managed.members or entity_id in managed.opaque_groups,
                members=tuple(sorted(managed.members.get(entity_id, ()))),
                covered_by=managed.covered_by.get(entity_id),
                excluded=managed.excluded.get(entity_id),
                override=rt.overrides.get(entity_id),
                conflicts=tuple(c.as_dict() for c in conflicts if c.enabled and c.automation not in acknowledged),
                acknowledged_conflicts=tuple(c.as_dict() for c in conflicts if c.automation in acknowledged),
                last_command=rt.last_commands.get(entity_id),
                targets=tuple(self.index.profile_targets(entity_id)),
            ))
        return out

    def resolve(self, mood: str, area_id: str | None, entity_id: str | None, targets,
                *, levels: tuple[str, ...] = ("global", "room", "light"), config: LightingConfig | None = None):
        """Resolved setting with the current global adjustment values filled in."""
        resolved = resolve(config or self.lighting, mood, area_id, entity_id, targets, levels=levels)
        return apply_global(resolved, self.adaptive.values)

    def plan(self, area_id: str, trigger: str = "preview", mood: str | None = None,
             config: LightingConfig | None = None, *, fresh: bool = False) -> Plan:
        """Plan for a room, including each light's next gradual update. Never sends anything.

        ``fresh`` re-evaluates daylight now (used right before executing); otherwise the
        latest daylight evaluation is used.
        """
        plan, _ = self._plan(area_id, trigger, mood, config, fresh=fresh)
        return plan

    def daylight_gate(self, area_id: str, *, fresh: bool = False) -> DaylightGate:
        return self.daylight.gate(area_id) if fresh else self.daylight.status(area_id).gate

    def _plan(self, area_id: str, trigger: str, mood: str | None = None,
              config: LightingConfig | None = None, *, fresh: bool = False,
              ) -> tuple[Plan, dict[str, tuple[Target, AdaptiveDecision]]]:
        lighting = config or self.lighting
        status = self.mood_status()
        if mood is None:
            mood, _ = self.room_mood(area_id, status)
        result = self.coordinator.occupancy.result(area_id)
        note = status.detail if status.manual or not mood else None
        targets = self._targets(area_id)
        plan = build_plan(
            area_id=area_id,
            mood=mood,
            targets=targets,
            resolve_fn=lambda t: self.resolve(mood or "", area_id, t.entity_id, t.targets, config=lighting),
            occupancy_state=result.state.value if result else None,
            occupied=result.occupied if result else None,
            trigger=trigger,
            control_enabled=self.room_active(area_id),
            note=note,
            manual_mode=status.manual,
            gate=self.daylight_gate(area_id, fresh=fresh),
        )
        rt = self.runtime.get(area_id) or RoomRuntime()
        now = time.monotonic()
        force = self._adaptive_force.get(area_id, set())
        competing = self.adaptive_conflicts(area_id)
        decisions: dict[str, tuple[Target, AdaptiveDecision]] = {}
        # A vacant room in its vacant mood (0.3.8): the current mood's global adjustment would undo it.
        vacant_mood = (result is not None and result.occupied is False
                       and lighting.room(area_id).vacancy.action == VacancyAction.VACANT_MOOD)
        for item, target in zip(plan.items, targets, strict=True):
            if vacant_mood and not item.persistent:
                continue
            if item.resolved.adaptive is None or not any(
                    e.get("mode") in ("global", "offset") for e in item.resolved.adaptive.values()):
                continue
            decision = adaptive_update(item, target, rt.adaptive_sent.get(item.entity_id), now,
                                       self.lighting.adaptive, force=item.entity_id in force,
                                       competing=competing.get(item.entity_id, ()))
            item.adaptive_update = decision.as_dict(now)
            decisions[item.entity_id] = (target, decision)
        return plan, decisions

    # ------------------------------------------------------------- triggers
    @callback
    def _schedule_apply(self, area_id: str, trigger: str, only: set[str] | None = None) -> None:
        """Coalesce triggers per room; apply after a short delay."""
        if not self._running or self._stopped or not self.room_active(area_id) or self._manual:
            return
        entry = self._pending.get(area_id)
        if entry is None:
            self._pending[area_id] = ({trigger}, set(only) if only is not None else None)
        else:
            triggers, current = entry
            triggers.add(trigger)
            # None means "every light"; it absorbs any restriction.
            merged = None if current is None or only is None else current | only
            self._pending[area_id] = (triggers, merged)
        if self._apply_timer is None:
            self._apply_timer = async_call_later(self.hass, APPLY_DEBOUNCE_SECONDS, self._run_pending)

    @callback
    def _run_pending(self, _now: datetime) -> None:
        self._apply_timer = None
        pending, self._pending = self._pending, {}
        for area_id, (triggers, only) in pending.items():
            trigger = next((t for t in ("room_mood_override", "mood_change", "activated", "occupancy_confirmed",
                                        "motion", "became_dark", "profile_change", "reconnected")
                            if t in triggers), sorted(triggers)[0])
            self._create_task(self.async_apply(area_id, trigger, only=only))

    def _create_task(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, f"smart_occupancy lighting {id(coro)}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _occupancy_listener(self, area_id: str) -> Callable[[], None]:
        @callback
        def update() -> None:
            result = self.coordinator.occupancy.result(area_id)
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            if result is None:
                return
            changed = (rt.occupancy_state, rt.occupied) != (result.state.value, result.occupied)
            was_occupied = rt.occupied
            was_state = rt.occupancy_state
            rt.occupancy_state, rt.occupied = result.state.value, result.occupied
            if not self._running:
                return
            waiting = was_state == OccupancyState.POSSIBLE_VACANCY
            if result.state == OccupancyState.POSSIBLE_VACANCY and not waiting and was_occupied:
                self._vacancy_started(area_id, result)
            elif waiting and result.occupied and result.state != OccupancyState.POSSIBLE_VACANCY:
                self._vacancy_cancelled(area_id)
            if result.occupied and rt.vacancy_deferred:
                rt.vacancy_deferred.clear()  # someone is here: a deferred vacancy action is no longer due
                self._mark_dirty()
            if result.state == OccupancyState.CONFIRMED_OCCUPANCY and not rt.visit_confirmed:
                rt.visit_confirmed = True
                new_visit = not rt.visit_started
                rt.visit_started = True
                if new_visit:
                    rt.visit += 1
                _LOGGER.debug("lighting_visit_confirmed area=%s", area_id)
                self._visit_trigger(area_id, "occupancy_confirmed")
            elif (not rt.visit_started and result.occupied
                  and result.state == OccupancyState.POSSIBLE_OCCUPANCY
                  and self.lighting.room(area_id).daylight.on_first_motion and _motion(result)):
                # A trigger sensor (PIR) saw motion: respond now, radar/presence keeps the room occupied.
                rt.visit_started = True
                rt.visit += 1
                _LOGGER.debug("lighting_visit_started area=%s reason=motion", area_id)
                self._visit_trigger(area_id, "motion")
            elif result.state == OccupancyState.VACANT and result.occupied is False:
                rt.visit_started = False
                if rt.visit_confirmed:
                    rt.visit_confirmed = False
                if was_occupied:  # (vacant again after e.g. a door-only movement: nothing ended)
                    if rt.vacancy_wait:
                        self.explain.vacancy_expired(area_id, rt.vacancy_wait[0])
                        rt.vacancy_wait = None
                    cleared = self._end_overrides(area_id, result)
                    self._vacate(area_id, "vacant", cleared=cleared)
            if changed:
                self._notify()

        return update

    # ----------------------------------------------------------- vacancy (0.3.8)
    def _lit(self, area_id: str) -> list[str]:
        return [e for e in self.managed_room(area_id).controlled if self.hass.states.is_state(e, "on")]

    @callback
    def _vacancy_started(self, area_id: str, result: RoomResult) -> None:
        """No presence any more: the vacancy timeout (the engine's possible-vacancy period) started."""
        rt = self.runtime_for(area_id)
        decision = self.explain.vacancy_wait(area_id, result, self._lit(area_id))
        rt.vacancy_wait = (decision.id, time.monotonic()) if decision else None

    @callback
    def _vacancy_cancelled(self, area_id: str) -> None:
        """Presence returned before the vacancy timeout ended: the vacancy action is not run."""
        rt = self.runtime_for(area_id)
        if rt.vacancy_wait:
            decision_id, since = rt.vacancy_wait
            self.explain.vacancy_cancelled(area_id, decision_id, time.monotonic() - since)
        rt.vacancy_wait = None
        _LOGGER.debug("lighting_vacancy_cancelled area=%s", area_id)

    def _end_overrides(self, area_id: str, result: RoomResult) -> int:
        """Manual overrides end with the vacancy, except one made while the room was already quiet.

        A change made after the last presence (nobody was detected in the room)
        was meant for the empty room - e.g. a light switched on from the app -
        so it is kept until it expires, and blocks the vacancy action for that
        light meanwhile. Returns how many overrides ended.
        """
        rt = self.runtime_for(area_id)
        if not rt.overrides:
            return 0
        quiet = result.quiet_since
        kept = {
            e: o for e, o in rt.overrides.items()
            if quiet is not None and (since := dt_util.parse_datetime(o.get("since") or "")) is not None
            and since > quiet
        }
        cleared = len(rt.overrides) - len(kept)
        if cleared:
            _LOGGER.debug("lighting_overrides_cleared area=%s reason=vacant n=%d kept=%d", area_id, cleared, len(kept))
            rt.overrides = kept
            self._mark_dirty()
            self._schedule_override_expiry()
        return cleared

    @callback
    def _vacate(self, area_id: str, trigger: str, *, only: set[str] | None = None, cleared: int = 0) -> None:
        if not self._running or self._stopped or area_id not in self.config.enabled_rooms:
            return
        self._create_task(self.async_vacate(area_id, trigger, only=only, cleared=cleared))

    def vacancy_plan(self, area_id: str, trigger: str = "vacant") -> Plan:
        """What the room's vacancy action does now. Never sends anything."""
        status = self.mood_status()
        mood, _ = self.room_mood(area_id, status)
        vacancy = self.lighting.room(area_id).vacancy
        result = self.coordinator.occupancy.result(area_id)
        return build_vacancy_plan(
            area_id=area_id,
            action=vacancy.action,
            mood=mood,
            vacant_mood=vacancy.mood,
            targets=self._targets(area_id),
            resolve_current=lambda t: self.resolve(mood or "", area_id, t.entity_id, t.targets),
            resolve_vacant=lambda t: self.resolve(vacancy.mood or "", area_id, t.entity_id, t.targets),
            occupancy_state=result.state.value if result else None,
            occupied=result.occupied if result else None,
            trigger=trigger,
            control_enabled=self.room_active(area_id),
            note=status.detail if status.manual or not mood else None,
            manual_mode=status.manual,
        )

    async def async_vacate(self, area_id: str, trigger: str = "vacant", *, only: set[str] | None = None,
                           cleared: int = 0) -> Plan | None:
        """Run the room's vacancy action (Automatic) or record what it would do (Observing, Manual)."""
        if self._stopped:
            return None
        lock = self._locks.setdefault(area_id, asyncio.Lock())
        async with lock:
            result = self.coordinator.occupancy.result(area_id)
            if self._stopped or result is None or result.state != OccupancyState.VACANT or result.occupied is not False:
                return None  # occupancy returned before this ran: there is nothing to vacate
            plan = self.vacancy_plan(area_id, trigger)
            rt = self.runtime_for(area_id)
            relevant = [i for i in plan.items if only is None or i.entity_id in only]
            changes = [i for i in relevant if i.decision == CHANGE]
            executing = plan.control_enabled and not plan.manual_mode and bool(changes)
            ok: list[PlanItem] | None = None
            errors: dict[str, str] = {}
            paused: set[str] = set()
            if executing:
                errors = await self._execute(area_id, changes)
                paused = {e for e, m in errors.items() if m == _PAUSED}
                errors = {e: m for e, m in errors.items() if m != _PAUSED}
                ok = [i for i in changes if i.entity_id not in errors and i.entity_id not in paused]
                sent_at = time.monotonic()
                for item in ok:
                    rt.last_commands[item.entity_id] = (item.service or "", dict(item.data))
                    rt.adaptive_sent[item.entity_id] = AdaptiveSent(sent_at, {
                        k: v for k, v in item.data.items() if k in ("brightness_pct", "color_temp_kelvin")})
            if plan.control_enabled and not plan.manual_mode:
                # Due later: lights kept by a manual override made in the empty room, unavailable or failed ones.
                handled = {i.entity_id for i in relevant}
                due = {i.entity_id for i in relevant if i.decision == BLOCKED
                       and i.reason_code in ("manual_override", "offline")} | set(errors)
                rt.vacancy_deferred = (rt.vacancy_deferred - handled) | due
            decision = self.explain.vacancy(area_id, trigger, plan, relevant, ok=ok, errors=errors, paused=paused,
                                            cleared=cleared)
            if decision is not None:
                rt.last_vacancy = {"at": decision.at, "action": plan.vacancy, "result": decision.status,
                                   "text": decision.explanation}
            log = _LOGGER.warning if errors else _LOGGER.info if executing else _LOGGER.debug
            log("lighting_vacancy area=%s action=%s trigger=%s changed=%d failed=%d deferred=%d", area_id,
                plan.vacancy, trigger, len(ok or []), len(errors), len(rt.vacancy_deferred))
            self._finish(area_id, plan)
            return plan

    @callback
    def _retry_vacancy(self, area_id: str, entities: set[str], trigger: str) -> None:
        """A deferred vacancy action becomes possible (override ended, light back) while the room is vacant."""
        rt = self.runtime.get(area_id)
        if rt is None or not (due := entities & rt.vacancy_deferred):
            return
        result = self.coordinator.occupancy.result(area_id)
        if result is None or result.state != OccupancyState.VACANT:
            return
        self._vacate(area_id, trigger, only=due)

    @callback
    def async_presence_changed(self) -> None:
        """Presence settings changed live (signals enabled or excluded, timing); occupancy was re-evaluated."""
        self._notify()

    @callback
    def _visit_trigger(self, area_id: str, trigger: str) -> None:
        """Arrival (or darkness) in a room: apply in an active room; otherwise record what would happen."""
        if self.room_active(area_id) and not self._manual:
            self._schedule_apply(area_id, trigger)
        else:
            # Kept in memory (saved with the next state write): no extra storage write per visit.
            plan = self.plan(area_id, trigger, fresh=True)
            self._record_activation(area_id, trigger, plan, None)
            self.explain.plan(area_id, trigger, plan, plan.items)
            self._notify()

    @callback
    def async_daylight_dark(self, area_id: str, status: DaylightStatus) -> None:
        """The daylight monitor's decision for a room became dark (after its stability period)."""
        if not self._running or self._stopped:
            return
        result = self.coordinator.occupancy.result(area_id)
        if result is None or not result.occupied or result.state not in (
                OccupancyState.CONFIRMED_OCCUPANCY, OccupancyState.POSSIBLE_OCCUPANCY):
            return
        _LOGGER.debug("lighting_became_dark area=%s %s", area_id, status.estimate.text)
        self._visit_trigger(area_id, "became_dark")

    def _record_activation(self, area_id: str, trigger: str, plan: Plan, switched_on: list[PlanItem] | None) -> None:
        """Why occupancy did (or did not) switch lights on. ``switched_on`` is None when nothing was executed."""
        if trigger not in _ACTIVATION_TRIGGERS:
            return
        rt = self.runtime.setdefault(area_id, RoomRuntime())
        candidates = [i for i in plan.items if i.service == "turn_on" and i.current.get("state") != "on"
                      and i.decision != "skipped"]
        gated = [i for i in candidates if i.reason_code in GATE_CODES]
        blocked = [i for i in candidates if i.decision == "blocked" and i.reason_code not in GATE_CODES]
        would = [i for i in candidates if i.decision == "change"]
        gate = plan.daylight
        word = _TRIGGER_WORDS.get(trigger, trigger)
        n = len(switched_on) if switched_on is not None else len(would)
        lights = f"{n} light{'s' if n != 1 else ''}"
        if plan.manual_mode:
            result, text = "manual", "Manual mode: nothing is switched on automatically"
        elif plan.mood is None:
            result, text = "no_mood", plan.note or "No effective mood: nothing is switched on"
        elif switched_on:
            result, text = "turned_on", f"Turned on {lights} ({word})"
        elif would and not plan.control_enabled:
            result, text = "observe", f"Observe: would turn on {lights} ({word})"
        elif would and switched_on is not None:
            n = len(would)
            result, text = "failed", f"Switching on {n} light{'s' if n != 1 else ''} failed ({word})"
        elif gated:
            code = gated[0].reason_code
            result = {GATE_NOT_NEEDED: "not_needed", GATE_UNCERTAIN: "uncertain", GATE_NEVER: "disabled"}[code]
            text = {"not_needed": "Not turned on: daylight is enough",
                    "uncertain": "Not turned on: daylight is uncertain (this room then leaves lights off)",
                    "disabled": "Not turned on: automatic switch-on is off for this room"}[result]
            if not plan.control_enabled:
                text = f"Observe: {text[0].lower()}{text[1:]}"
        elif blocked:
            reasons = sorted({i.reason_code.replace("_", " ") for i in blocked})
            result, text = "blocked", f"Not turned on: {', '.join(reasons)}"
        elif any(i.service == "turn_on" for i in plan.items):
            result, text = "already_on", "Lights were already on"
        else:
            result, text = "nothing", f"No light switches on in {plan.mood}"
        if gate is not None and gate.policy == AutoOn.DARK and result not in ("manual", "no_mood"):
            text += f" · {gate.decision.value.capitalize()}: {gate.text}"
        same_visit = rt.activation is not None and rt.activation.get("visit") == rt.visit
        if switched_on or trigger == "became_dark" or not same_visit or rt.activation.get("result") != "turned_on":
            rt.activation = {"at": _iso(dt_util.utcnow()), "trigger": trigger, "result": result, "text": text,
                             "lights": n, "visit": rt.visit,
                             "daylight": gate.decision.value if gate else None}
        if switched_on:
            rt.last_activation_at = rt.activation["at"]

    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self.config.mood.entity_ids:
            self._on_mood_event()
        if entity_id in self._switch_rooms:
            self._on_switch_event(event)
        if entity_id in self._light_targets:
            self._on_light_event(event)

    @callback
    def _on_mood_event(self) -> None:
        status = self.mood_status()
        if status.manual != self._manual:
            self._manual = status.manual
            self.coordinator.async_schedule_refresh()
            if status.manual:
                self._pause()
            else:
                _LOGGER.info("lighting_manual_mode_ended mood=%s", status.mood)
        if status.mood == self._last_mood:
            if status.manual:
                self._notify()
            return
        old, self._last_mood = self._last_mood, status.mood
        _LOGGER.debug("lighting_mood_changed old=%s new=%s source=%s", old, status.mood, status.source)
        if status.mood is not None:
            for area_id in self.lighting.active_rooms:
                if not self.lighting.room(area_id).mood_override:
                    self._schedule_apply(area_id, "mood_change")
            if self._running:
                # Observing rooms: what the new mood would do (evaluated, logged, nothing is sent).
                for area_id in sorted(self.config.enabled_rooms):
                    if not self.room_active(area_id) and not self.lighting.room(area_id).mood_override:
                        self._observe(area_id, "mood_change")
        self._notify()

    @callback
    def _observe(self, area_id: str, trigger: str) -> None:
        """Evaluate a trigger in an Observing room and log what would happen. Never sends anything."""
        plan = self.plan(area_id, trigger)
        self.explain.plan(area_id, trigger, plan, plan.items)

    @callback
    def _pause(self) -> None:
        """Manual mode started: drop every queued application and gradual update (never replayed)."""
        dropped = len(self._pending) + len(self._adaptive_timers)
        if self._apply_timer:
            self._apply_timer()
            self._apply_timer = None
        for area_id, (triggers, _only) in self._pending.items():
            self.explain.paused(area_id, triggers)
        self._pending.clear()
        self._cancel_adaptive()
        _LOGGER.info("lighting_manual_mode_started dropped_pending=%d (%s)", dropped, MANUAL_PAUSED_TEXT)
        self._notify()

    # ------------------------------------------------------ global adjustment
    @callback
    def _on_global_change(self, old: GlobalValues, new: GlobalValues) -> None:
        """A global source value changed: lights that follow it get a gradual update."""
        # A source that is an Adaptive Lighting switch may have been switched on or off.
        self._source_controllers = None
        self._adaptive_conflicts = None
        if self._running and not self._stopped and not self._manual:
            for area_id in self.lighting.active_rooms:
                if area_id in self.config.enabled_rooms:
                    self._schedule_adaptive(area_id)
        self._mark_dirty()
        self._notify()

    @callback
    def _schedule_adaptive(self, area_id: str, delay: float = ADAPTIVE_DEBOUNCE_SECONDS,
                           force: set[str] | None = None) -> None:
        """Coalesce gradual updates per room; the newest values are read when the update runs."""
        if not self._running or self._stopped or self._manual or not self.room_active(area_id):
            return
        if force:
            self._adaptive_force.setdefault(area_id, set()).update(force)
        due = time.monotonic() + delay
        current = self._adaptive_timers.get(area_id)
        if current is not None:
            if current[1] <= due:
                return
            current[0]()

        @callback
        def fire(_now: datetime) -> None:
            self._adaptive_timers.pop(area_id, None)
            self._create_task(self.async_adaptive(area_id))

        self._adaptive_timers[area_id] = (async_call_later(self.hass, delay, fire), due)

    def _cancel_adaptive(self) -> None:
        for cancel, _due in self._adaptive_timers.values():
            cancel()
        self._adaptive_timers.clear()
        self._adaptive_force.clear()

    async def async_adaptive(self, area_id: str) -> dict[str, AdaptiveDecision] | None:
        """Send the gradual updates of one room (lights already on only). Records the outcome."""
        if self._stopped or self._manual or not self.room_active(area_id):
            return None
        lock = self._locks.setdefault(area_id, asyncio.Lock())
        async with lock:
            if self._stopped or self._manual or not self.room_active(area_id):
                return None
            _plan, decisions = self._plan(area_id, "adaptive")
            forced = self._adaptive_force.pop(area_id, None) or set()
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            sends = {e: d for e, (_t, d) in decisions.items() if d.decision == SEND}
            deferred = [d.due for _t, d in decisions.values() if d.decision == DEFERRED and d.due]
            if sends:
                assert_room_control_permitted(self.room_active(area_id), area_id)
                errors = await self._send(area_id, "turn_on", {e: d.data for e, d in sends.items()})
                now = time.monotonic()
                for entity_id, d in sends.items():
                    if entity_id in errors:
                        continue
                    fields = {k: v for k, v in d.data.items() if k != "transition"}
                    rt.adaptive_sent[entity_id] = AdaptiveSent(now, fields)
                    if prev := rt.last_commands.get(entity_id):
                        rt.last_commands[entity_id] = (prev[0], {**prev[1], **fields})
                ok = len(sends) - len([e for e in errors if errors[e] != _PAUSED])
                sent_data = {e: d.data for e, d in sends.items() if errors.get(e) != _PAUSED}
                if sent_data:
                    self.explain.adaptive(area_id, sent_data,
                                          {e: m for e, m in errors.items() if m != _PAUSED}, forced)
                rt.last_adaptive_at = _iso(dt_util.utcnow())
                rt.last_adaptive = f"{ok} light(s) followed the global adjustment" + (
                    f", {len(errors)} failed: " + "; ".join(f"{e}: {m}" for e, m in errors.items())
                    if errors else "")
                _LOGGER.debug("lighting_adaptive area=%s sent=%d failed=%d deferred=%d",
                              area_id, ok, len(errors), len(deferred))
                self._mark_dirty()
            if deferred:
                self._schedule_adaptive(area_id, max(0.5, min(deferred) - time.monotonic()))
            self._notify()
            return {e: d for e, (_t, d) in decisions.items()}

    @callback
    def _on_switch_event(self, event: Event[EventStateChangedData]) -> None:
        now = time.monotonic()
        ctx = event.context
        self._switch_contexts[ctx.id] = now
        for area_id in self._switch_rooms.get(event.data["entity_id"], ()):
            self._switch_seen[area_id] = (now, ctx.id)
        self._prune(now)

    def _prune(self, now: float) -> None:
        horizon = now - max(OWN_CHANGE_WINDOW_SECONDS, 60)
        for store in (self._switch_contexts, self._own_contexts, self._switch_commands):
            for key in [k for k, t in store.items() if t < horizon]:
                del store[key]
        for key in [k for k, (until, _) in self._expected.items() if until < now]:
            del self._expected[key]

    def _classify(self, area_id: str, ctx: Context, now: float, echo: bool) -> str:
        """Who changed a light: ui, wall_switch, ours (echo), automation or unattributed."""
        if ctx.user_id:
            return "ui"
        if ctx.id in self._switch_contexts:
            return "wall_switch"
        if ctx.parent_id and ctx.parent_id in self._switch_contexts:
            return "wall_switch"
        seen = self._switch_seen.get(area_id)
        if seen and now - seen[0] <= SWITCH_ATTRIBUTION_SECONDS and not ctx.parent_id:
            return "wall_switch"
        if echo:
            return "ours"
        if ctx.parent_id:
            return "automation"
        return "unattributed"

    @callback
    def _on_light_event(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        entity_id = event.data["entity_id"]
        if old is None or new is None:
            self._invalidate_managed()
            return
        if old.state in _UNAVAILABLE or new.state in _UNAVAILABLE:
            if (old.state in _UNAVAILABLE) != (new.state in _UNAVAILABLE):
                self.coordinator.async_schedule_refresh()
                self._notify()
            if old.state in _UNAVAILABLE and new.state not in _UNAVAILABLE:
                for area_id, target in self._light_targets.get(entity_id, ()):
                    rt = self.runtime.get(area_id)
                    if rt and target in rt.pending_reconnect:
                        self._schedule_apply(area_id, "reconnected", {target})
                    if rt and target in rt.vacancy_deferred and self.room_active(area_id) and not self._manual:
                        self._retry_vacancy(area_id, {target}, "reconnected")
            return
        if old.attributes.get("entity_id") != new.attributes.get("entity_id"):
            self._invalidate_managed()  # group membership changed
        if not _significant(old, new):
            return
        ctx = new.context
        now = time.monotonic()
        self._prune(now)
        if ctx.id in self._own_contexts or (ctx.parent_id and ctx.parent_id in self._own_contexts):
            return
        if ctx.id in self._switch_commands:
            return  # the report of a wall switch command: its override is already registered
        expected = self._expected.get(entity_id)
        # Our command reported back with a new context (e.g. MQTT): same direction, shortly after.
        echo = bool(expected and expected[0] >= now and (expected[1] == "turn_off") == (new.state == "off"))
        switched_on = old.state != "on" and new.state == "on"
        for area_id, target in self._light_targets.get(entity_id, ()):
            source = self._classify(area_id, ctx, now, echo)
            if source == "ours":
                continue
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            rt.last_commands.pop(target, None)
            rt.adaptive_sent.pop(target, None)
            rt.last_external[target] = {
                "at": _iso(dt_util.utcnow()), "source": source, "state": new.state, "entity_id": entity_id,
            }
            manual = source in ("ui", "wall_switch") or (
                source == "unattributed" and self.lighting.unattributed == UnattributedPolicy.MANUAL
            )
            if not manual and switched_on and target == entity_id:
                # Switched on by something else: give it the current global values right away.
                self._schedule_adaptive(area_id, ADAPTIVE_DEBOUNCE_SECONDS, {target})
            existing = rt.overrides.get(target)
            if source == "wall_switch" and existing and existing.get("switch_id") and \
                    existing.get("state") == new.state:
                manual = False  # a wall switch command reported back: keep its (more detailed) override
            if manual and self.room_active(area_id) and not self._manual:
                # (In Manual mode a person's change is expected; nothing would be corrected anyway.)
                minutes = self.lighting.override_minutes(area_id)
                now_dt = dt_util.utcnow()
                rt.overrides[target] = {
                    "entity_id": target,
                    "source": source,
                    "since": _iso(now_dt),
                    "until": _iso(now_dt + timedelta(minutes=minutes)) if minutes else None,
                    "user_id": ctx.user_id,
                    "state": new.state,
                }
                _LOGGER.info("lighting_manual_override area=%s light=%s source=%s until=%s",
                             area_id, target, source, rt.overrides[target]["until"])
                self.explain.override(area_id, [target], source, new.state, minutes)
                self._mark_dirty()
                self._schedule_override_expiry()
            self._notify()

    # -------------------------------------------------------- manual overrides
    @callback
    def _schedule_override_expiry(self) -> None:
        if self._override_timer:
            self._override_timer()
            self._override_timer = None
        if self._stopped:
            return
        times = [
            dt for rt in self.runtime.values() for o in rt.overrides.values()
            if o.get("until") and (dt := dt_util.parse_datetime(o["until"])) is not None
        ]
        if times:
            self._override_timer = async_track_point_in_utc_time(self.hass, self._expire_overrides, min(times))

    @callback
    def _expire_overrides(self, now: datetime) -> None:
        self._override_timer = None
        changed = False
        for area_id, rt in self.runtime.items():
            expired = [e for e, o in rt.overrides.items()
                       if o.get("until") and (dt_util.parse_datetime(o["until"]) or now) <= now]
            for entity_id in expired:
                del rt.overrides[entity_id]
                _LOGGER.debug("lighting_override_expired area=%s light=%s", area_id, entity_id)
                changed = True
            if expired:
                self._retry_vacancy(area_id, set(expired), "override_expired")
        if changed:
            self._mark_dirty()
            self._notify()
        self._schedule_override_expiry()

    @callback
    def async_clear_override(self, area_id: str, entity_id: str | None = None) -> int:
        rt = self.runtime.get(area_id)
        if rt is None:
            return 0
        if entity_id is None:
            ended = set(rt.overrides)
            rt.overrides.clear()
        else:
            ended = {entity_id} if rt.overrides.pop(entity_id, None) else set()
        n = len(ended)
        if ended:
            self._retry_vacancy(area_id, ended, "override_expired")
        if n:
            self._mark_dirty()
            self._schedule_override_expiry()
            self._notify()
        return n

    # ------------------------------------------------------------ execution
    async def async_apply(self, area_id: str, trigger: str, *, only: set[str] | None = None) -> Plan | None:
        """Plan and, in an activated room, execute. Records the outcome."""
        if self._stopped or self._manual or not self.room_active(area_id):
            return None
        lock = self._locks.setdefault(area_id, asyncio.Lock())
        async with lock:
            if self._manual:
                return None  # Manual started while this application was queued: not replayed
            plan = self.plan(area_id, trigger, fresh=True)
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            changes = [i for i in plan.changes if only is None or i.entity_id in only]
            relevant = [i for i in plan.items if only is None or i.entity_id in only]
            rt.pending_reconnect = {
                i.entity_id for i in plan.items if i.decision == BLOCKED and i.reason_code == "offline"
            }
            now = dt_util.utcnow()
            rt.last_plan = plan
            rt.last_trigger = trigger
            rt.last_decision_at = _iso(now)
            counts = {d: sum(1 for i in relevant if i.decision == d) for d in (CHANGE, "unchanged", BLOCKED, "skipped")}
            rt.last_counts = counts
            if plan.mood is None:
                rt.last_result, rt.last_reason, rt.last_errors = "no_mood", plan.note or "No effective mood", []
                self._record_activation(area_id, trigger, plan, [])
                self.explain.plan(area_id, trigger, plan, relevant)
                self._finish(area_id, plan)
                return plan
            if not changes:
                rt.last_result = "no_change"
                rt.last_reason = _summary(plan.mood, relevant, trigger)
                rt.last_errors = []
                self._record_activation(area_id, trigger, plan, [])
                self.explain.plan(area_id, trigger, plan, relevant)
                self._finish(area_id, plan)
                return plan
            errors = await self._execute(area_id, changes)
            paused = {e for e, m in errors.items() if m == _PAUSED}
            errors = {e: m for e, m in errors.items() if m != _PAUSED}
            ok = [i for i in changes if i.entity_id not in errors and i.entity_id not in paused]
            sent_at = time.monotonic()
            for item in ok:
                rt.last_commands[item.entity_id] = (item.service or "", dict(item.data))
                rt.adaptive_sent[item.entity_id] = AdaptiveSent(sent_at, {
                    k: v for k, v in item.data.items() if k in ("brightness_pct", "color_temp_kelvin")})
            rt.last_errors = [f"{e}: {msg}" for e, msg in errors.items()]
            rt.last_result = "applied" if not errors else ("partial" if ok else "failed")
            rt.last_reason = _summary(plan.mood, relevant, trigger, failed=len(errors))
            if ok:
                rt.last_applied_mood = plan.mood
                rt.last_applied_at = _iso(dt_util.utcnow())
            self._record_activation(area_id, trigger, plan, [
                i for i in ok if i.service == "turn_on" and i.current.get("state") != "on"])
            self.explain.plan(area_id, trigger, plan, relevant, ok=ok, errors=errors, paused=paused)
            log = _LOGGER.warning if errors else _LOGGER.info
            log("lighting_applied area=%s mood=%s trigger=%s result=%s changed=%d failed=%d",
                area_id, plan.mood, trigger, rt.last_result, len(ok), len(errors))
            self._finish(area_id, plan)
            return plan

    def _finish(self, area_id: str, plan: Plan) -> None:
        for item in plan.items:
            _LOGGER.debug("lighting_decision area=%s light=%s decision=%s reason=%s",
                          area_id, item.entity_id, item.decision, item.reason_code)
        self._mark_dirty()
        self.coordinator.async_schedule_refresh()
        self._notify()

    async def _execute(self, area_id: str, items: list[PlanItem]) -> dict[str, str]:
        """Send the mood commands, batched by identical service data. Returns {entity: error}."""
        assert_room_control_permitted(self.room_active(area_id), area_id)
        by_service: dict[str, dict[str, dict[str, Any]]] = {}
        for item in items:
            by_service.setdefault(item.service or "", {})[item.entity_id] = item.data
        errors: dict[str, str] = {}
        for service, data in by_service.items():
            errors |= await self._send(area_id, service, data)
        return errors

    async def _send(self, area_id: str, service: str, data_by_entity: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Send one light service to several lights, batched by identical data. Returns {entity: error}.

        Stops before every batch if Manual mode started (those lights are reported as paused).
        """
        batches: dict[str, list[str]] = {}
        for entity_id, data in data_by_entity.items():
            batches.setdefault(json.dumps(data, sort_keys=True), []).append(entity_id)
        context = Context()
        now = time.monotonic()
        self._own_contexts[context.id] = now
        errors: dict[str, str] = {}
        for data_json, entity_ids in batches.items():
            if self._manual or self._stopped:
                errors.update({e: _PAUSED for e in entity_ids})
                continue
            payload = json.loads(data_json)
            window = OWN_CHANGE_WINDOW_SECONDS + float(payload.get("transition") or 0)
            for entity_id in entity_ids:
                self._expected[entity_id] = (now + window, service)
            data = {**payload, "entity_id": entity_ids}
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                    await self.hass.services.async_call("light", service, data, blocking=True, context=context)
            except TimeoutError:
                errors.update({e: "no response within timeout" for e in entity_ids})
            except (HomeAssistantError, ValueError) as err:
                errors.update({e: str(err) or type(err).__name__ for e in entity_ids})
            _LOGGER.debug("lighting_command area=%s service=light.%s data=%s", area_id, service, data)
        return errors

    # ------------------------------------------------------ configuration
    @callback
    def _on_lighting_config_changed(self, old: LightingConfig, new: LightingConfig) -> None:
        self._invalidate_managed()
        self.daylight.async_config_changed()
        self.coordinator.async_schedule_refresh()
        if old.adaptive != new.adaptive:
            self._source_controllers = None
            self.adaptive.async_configure(new.adaptive)
        if old.switch_settings != new.switch_settings or any(
                old.room(a).switches != new.room(a).switches for a in {*old.rooms, *new.rooms}):
            self.switch_inputs.async_config_changed()
            # A removed switch's log goes with it; rooms keep theirs.
            self.decisions.prune(lambda s: "/" not in s or new.room(s.split("/", 1)[0]).switch(
                s.split("/", 1)[1]) is not None)
        if self._running and (old.unrepresentable != new.unrepresentable or old.manual_option != new.manual_option):
            # The handling of a mood Main Mood cannot represent changes the effective mood.
            self._on_mood_event()
        if self._running:
            for area_id in self.config.enabled_rooms:
                o, n = old.room(area_id), new.room(area_id)
                if not n.control_enabled:
                    if o.control_enabled:
                        self.explain.deactivated(area_id)
                    elif o.mood_override != n.mood_override:
                        self._observe(area_id, "room_mood_override")
                    continue
                if not o.control_enabled:
                    self._schedule_apply(area_id, "activated")
                    continue
                if o.mood_override != n.mood_override:
                    self._schedule_apply(area_id, "room_mood_override")
                    continue
                changed = self._changed_targets(area_id, old, new)
                if changed:
                    self._schedule_apply(area_id, "profile_change", changed)
        self._notify()

    def _changed_targets(self, area_id: str, old: LightingConfig, new: LightingConfig) -> set[str]:
        """Lights whose resolved setting in the room's current mood changed."""
        mood, _ = self.room_mood(area_id)
        if not mood:
            return set()
        out: set[str] = set()
        for entity_id in self.managed_room(area_id).controlled:
            targets = self.index.profile_targets(entity_id)
            if resolve(old, mood, area_id, entity_id, targets).as_dict() != \
                    resolve(new, mood, area_id, entity_id, targets).as_dict():
                out.add(entity_id)
        return out

    # -------------------------------------------------------- diagnostics
    def room_diagnostics(self, area_id: str) -> dict[str, Any]:
        status = self.mood_status()
        mood, mood_source = self.room_mood(area_id, status)
        room_lighting = self.lighting.room(area_id)
        managed = self.managed_room(area_id)
        rt = self.runtime.get(area_id) or RoomRuntime()
        active = self.room_active(area_id)
        conflicts = self.conflicts(area_id)
        acknowledged = set(room_lighting.acknowledged)
        blocking = sorted({c.automation for cs in conflicts.values() for c in cs
                           if c.enabled and c.automation not in acknowledged})
        controlled = managed.controlled
        offline = [e for e in controlled if (s := self.hass.states.get(e)) is None or s.state in _UNAVAILABLE]
        unsupported: list[dict[str, Any]] = []
        if mood:
            plan = self.plan(area_id, "preview", mood)
            unsupported = [
                {"entity_id": i.entity_id, "name": i.name, "notes": i.unsupported}
                for i in plan.items if i.decision != "skipped"
                and any(n.get("level") == "warning" for n in i.unsupported)
            ]
        if not active:
            mode = LightingMode.OBSERVE
            mode_reason = "Observe: no light is controlled in this room"
        elif status.manual:
            mode = LightingMode.PAUSED
            mode_reason = MANUAL_PAUSED_TEXT
        elif controlled and all(self.blocking_conflicts(area_id, e) for e in controlled):
            mode = LightingMode.BLOCKED
            mode_reason = "Every managed light is also controlled by an automation that is not acknowledged"
        elif not controlled:
            mode = LightingMode.BLOCKED
            mode_reason = "No light can be controlled (none managed, or all excluded)"
        else:
            mode = LightingMode.ACTIVE
            mode_reason = "Mood lighting is active"
        return {
            "area_id": area_id,
            "room": area_name(self.hass, area_id) or area_id,
            "control_mode": mode.value,
            "control_mode_reason": mode_reason,
            "control_enabled": room_lighting.control_enabled,
            "activated_at": room_lighting.activated_at,
            "global_mood": status.mood,
            "global_mood_detail": status.detail,
            "mood_source": status.source,
            "room_mood": mood,
            "follows_global": mood_source == "global",
            "room_mood_override": room_lighting.mood_override,
            "room_profile_overrides": sum(len(t) for t in room_lighting.mood_overrides.values())
            + sum(len(t) for t in room_lighting.light_overrides.values()),
            "managed_lights": len(controlled),
            "managed": list(managed.targets),
            "discovered": sorted(managed.discovered),
            "excluded": [{"entity_id": e, "reason": r} for e, r in managed.excluded.items()],
            "covered_by_group": dict(managed.covered_by),
            "opaque_groups": list(managed.opaque_groups),
            "offline_lights": offline,
            "unsupported": unsupported,
            "conflicts": sorted(
                ({**c.as_dict(), "acknowledged": c.automation in acknowledged}
                 for cs in conflicts.values() for c in cs),
                key=lambda c: (c["acknowledged"], not c["enabled"], c["automation"], c["light"]),
            ),
            "blocking_automations": blocking,
            "manual_overrides": list(rt.overrides.values()),
            "manual_override_minutes": self.lighting.override_minutes(area_id),
            "last_applied_profile": rt.last_applied_mood,
            "last_applied_at": rt.last_applied_at,
            "last_trigger": rt.last_trigger,
            "last_result": rt.last_result,
            "last_reason": rt.last_reason,
            "last_counts": rt.last_counts,
            "last_errors": rt.last_errors,
            "last_decision_at": rt.last_decision_at,
            "manual_mode": status.manual,
            "global_adjustment": self.adaptive.values.status,
            "last_adaptive_at": rt.last_adaptive_at,
            "last_adaptive": rt.last_adaptive,
            "adaptive_conflicts": {e: [dict(c) for c in cs] for e, cs in self.adaptive_conflicts(area_id).items()},
            "auto_on": {
                "policy": room_lighting.daylight.auto_on.value,
                "on_first_motion": room_lighting.daylight.on_first_motion,
                "last_activation_at": rt.last_activation_at,
                "activation": rt.activation,
            },
            "last_external_changes": rt.last_external,
            "vacancy": {
                **room_lighting.vacancy.as_dict(),
                "last": rt.last_vacancy,
                "deferred": sorted(rt.vacancy_deferred),
            },
        }

    def activation_review(self, area_id: str) -> dict[str, Any]:
        """What enabling lighting control would mean for a room."""
        managed = self.managed_room(area_id)
        conflicts = self.conflicts(area_id)
        acknowledged = set(self.lighting.room(area_id).acknowledged)
        automations: dict[str, dict[str, Any]] = {}
        for cs in conflicts.values():
            for c in cs:
                a = automations.setdefault(c.automation, {
                    "automation": c.automation, "name": c.name, "enabled": c.enabled,
                    "acknowledged": c.automation in acknowledged, "lights": [],
                })
                a["lights"].append({"entity_id": c.light, "via": c.via, "detail": c.detail})
        return {
            "area_id": area_id,
            "room": area_name(self.hass, area_id) or area_id,
            "room_enabled": area_id in self.config.enabled_rooms,
            "control_enabled": self.lighting.room(area_id).control_enabled,
            "managed": [
                {"entity_id": e, "name": friendly_name(self.hass, e), "category": self.index.category_of(e),
                 "explicit": e in managed.explicit, "discovered": e in managed.discovered}
                for e in managed.controlled
            ],
            "excluded": [{"entity_id": e, "name": friendly_name(self.hass, e), "reason": r}
                         for e, r in managed.excluded.items()],
            "covered_by_group": [{"entity_id": m, "group": g} for m, g in managed.covered_by.items()],
            "opaque_groups": managed.opaque_groups,
            "automations": sorted(automations.values(), key=lambda a: (not a["enabled"], a["automation"])),
            "plan": self.plan(area_id, "activated").as_dict(),
        }

    # -------------------------------------------------------------- helpers
    @callback
    def _notify(self) -> None:
        if not self._stopped:
            async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)

    @callback
    def _on_decision(self) -> None:
        """A decision was recorded or updated: saved with the next (delayed) state write; cards refetch."""
        self._mark_dirty()
        self._notify()

    @callback
    def _mark_dirty(self) -> None:
        self._dirty = True
        self._state_store.async_delay_save(self._state_data, LIGHTING_STATE_SAVE_DELAY_SECONDS)

    def _state_data(self) -> dict[str, Any]:
        self._dirty = False
        return {"rooms": {a: rt.persist() for a, rt in self.runtime.items()}, "adaptive": self.adaptive.persist(),
                "daylight": self.daylight.persist(), "switches": self.switch_inputs.persist(),
                "decisions": self.decisions.persist()}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mood": self.mood_status().as_dict(),
            "lighting_config": self.lighting.as_dict(),
            "global_adjustment": self.adaptive.values.as_dict(),
            "source_controllers": [c.as_dict() for c in self.source_controllers()],
            "rooms": {a: self.room_diagnostics(a) for a in sorted(self.config.enabled_rooms)},
            "tracked_entities": list(self._tracked),
            "daylight": self.daylight.diagnostics(),
            "switches": {
                "mqtt": self.switch_inputs.mqtt_state,
                "bridge_devices": len(self.switch_inputs.bridge_devices),
                "rooms": {f"{a}/{s}": st.as_dict() for (a, s), st in self.switch_inputs.status.items()},
            },
        }

    def with_room(self, area_id: str, **changes: Any) -> LightingConfig:
        return self.lighting.with_room(area_id, replace(self.lighting.room(area_id), **changes))

    # ---------------------------------------------------------- wall switches
    @callback
    def notify(self) -> None:
        self._notify()

    @callback
    def mark_dirty(self) -> None:
        self._mark_dirty()

    def runtime_for(self, area_id: str) -> RoomRuntime:
        return self.runtime.setdefault(area_id, RoomRuntime())

    @callback
    def _on_button(self, event: ButtonEvent) -> None:
        self.switch_control.async_handle(event)

    @callback
    def note_switch_press(self, area_id: str) -> Context:
        """A physical switch of this room was pressed: light changes right after it are a person's.

        Returns the context for the commands this press may send (their light
        changes are attributed to the wall switch, and scenes or scripts it
        starts too).
        """
        context = Context()
        now = time.monotonic()
        self._switch_contexts[context.id] = now
        self._switch_seen[area_id] = (now, context.id)
        self._prune(now)
        return context

    async def async_switch_lights(self, area_id: str, service: str, data_by_entity: dict[str, dict[str, Any]],
                                  context: Context, *, switch_id: str) -> dict[str, str]:
        """Send a light command for a wall switch press. Returns {entity: error}.

        Only for a switch set to Active (a switch in Observing never sends).
        Unlike automatic lighting it is neither blocked by the room's
        Observing mode nor paused by Manual mood: it is a person's request.
        """
        assert_switch_control_permitted(self.switch_active(area_id, switch_id), area_id, switch_id)
        if self._stopped:
            return {e: "stopped" for e in data_by_entity}
        batches: dict[str, list[str]] = {}
        for entity_id, data in data_by_entity.items():
            batches.setdefault(json.dumps(data, sort_keys=True), []).append(entity_id)
        now = time.monotonic()
        self._switch_commands[context.id] = now
        errors: dict[str, str] = {}
        for data_json, entity_ids in batches.items():
            payload = json.loads(data_json)
            window = OWN_CHANGE_WINDOW_SECONDS + float(payload.get("transition") or 0)
            for entity_id in entity_ids:
                self._expected[entity_id] = (now + window, service)
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                    await self.hass.services.async_call(
                        "light", service, {**payload, "entity_id": entity_ids}, blocking=True, context=context)
            except TimeoutError:
                errors.update({e: "no response within timeout" for e in entity_ids})
            except (HomeAssistantError, ValueError) as err:
                errors.update({e: str(err) or type(err).__name__ for e in entity_ids})
            _LOGGER.debug("switch_light_command area=%s service=light.%s entities=%s data=%s", area_id, service,
                          entity_ids, payload)
        return errors

    @callback
    def register_switch_override(self, area_id: str, entities: list[str], state: str, *, switch: SwitchConfig,
                                 raw: str, reason: str) -> list[str]:
        """Manual override for the managed lights (or their managed groups) a switch press changed."""
        targets: list[str] = []
        for entity_id in entities:
            for area, target in self._light_targets.get(entity_id, ()):
                if area == area_id and target not in targets:
                    targets.append(target)
        if not targets:
            return []
        rt = self.runtime_for(area_id)
        minutes = switch.override_minutes if switch.override_minutes is not None else \
            self.lighting.override_minutes(area_id)
        now_dt = dt_util.utcnow()
        for target in targets:
            rt.last_commands.pop(target, None)
            rt.adaptive_sent.pop(target, None)
            rt.overrides[target] = {
                "entity_id": target, "source": "wall_switch", "since": _iso(now_dt),
                "until": _iso(now_dt + timedelta(minutes=minutes)) if minutes else None,
                "user_id": None, "state": state, "switch": switch.name, "switch_id": switch.id,
                "action": raw, "reason": reason,
            }
        _LOGGER.info("lighting_manual_override area=%s lights=%s source=wall_switch switch=%s action=%s",
                     area_id, ",".join(targets), switch.id, raw)
        self.explain.override(area_id, targets, "wall_switch", state, minutes, switch=switch.name, action=raw)
        self._mark_dirty()
        self._schedule_override_expiry()
        self._notify()
        return targets


def _summary(mood: str | None, items, trigger: str, *, failed: int = 0) -> str:
    counts: dict[str, int] = {}
    blocked: dict[str, int] = {}
    for item in items:
        counts[item.decision] = counts.get(item.decision, 0) + 1
        if item.decision == BLOCKED:
            blocked[item.reason_code] = blocked.get(item.reason_code, 0) + 1
    words = {
        "offline": "offline", "manual_override": "manual override", "conflict": "automation conflict",
        "vacant": "room vacant", "occupancy_uncertain": "occupancy uncertain",
        "off_needs_confirmation": "occupancy not confirmed",
        GATE_NOT_NEEDED: "not needed (daylight)", GATE_UNCERTAIN: "daylight uncertain",
        GATE_NEVER: "automatic switch-on off",
    }
    parts = [f"{mood} ({trigger.replace('_', ' ')})"]
    if n := counts.get(CHANGE, 0) - failed:
        parts.append(f"{n} changed")
    if failed:
        parts.append(f"{failed} failed")
    if n := counts.get("unchanged"):
        parts.append(f"{n} already correct")
    if blocked:
        parts.append("blocked: " + ", ".join(f"{n} {words.get(c, c)}" for c, n in blocked.items()))
    if n := counts.get("skipped"):
        parts.append(f"{n} not controlled")
    return " · ".join(parts)


def _motion(result: RoomResult) -> bool:
    """A trigger sensor (PIR or other motion, not a door) is active now."""
    return any(v.track.is_trigger and v.status == SignalStatus.ACTIVE for v in result.signals)
