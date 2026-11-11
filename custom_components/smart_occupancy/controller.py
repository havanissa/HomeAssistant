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
from .conflicts import AutomationInfo, Conflict, collect_automations, find_conflicts
from .const import (
    APPLY_DEBOUNCE_SECONDS,
    COMMAND_TIMEOUT_SECONDS,
    LIGHTING_STATE_SAVE_DELAY_SECONDS,
    LIGHTING_STATE_STORE_KEY,
    LIGHTING_STATE_STORE_VERSION,
    OWN_CHANGE_WINDOW_SECONDS,
    SIGNAL_LIGHTING_UPDATED,
    SWITCH_ATTRIBUTION_SECONDS,
    LightingMode,
    OccupancyState,
    UnattributedPolicy,
    UnrepresentableMode,
)
from .discovery import area_name
from .lighting import LightIndex, expand_group, light_capabilities
from .lighting_config import LightingConfig, LightingStore
from .models import IntegrationConfig
from .occupancy import friendly_name
from .planner import BLOCKED, CHANGE, Plan, Target, build_plan
from .profiles import resolve
from .supervision import assert_room_control_permitted

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

_LOGGER = logging.getLogger(__name__)

_UNAVAILABLE = (STATE_UNAVAILABLE, STATE_UNKNOWN)
_SIGNIFICANT_ATTRS = ("brightness", "color_temp_kelvin", "rgb_color", "hs_color", "xy_color", "effect", "color_mode")
_AUTOMATION_DEBOUNCE_SECONDS = 2.0


@dataclass(slots=True, frozen=True)
class MoodStatus:
    """The effective global mood for lighting, and how it was determined."""

    mood: str | None
    source: str  # main | follow_main | no_mood | use_mood | main_unavailable | not_configured
    main_mood: str | None
    expected: str | None
    follows: str | None  # "auto" | "manual"
    unrepresentable: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
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
        }

    @classmethod
    def restore(cls, data: Any) -> RoomRuntime:
        data = data if isinstance(data, dict) else {}
        commands = data.get("last_commands") if isinstance(data.get("last_commands"), dict) else {}
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
        self._running = False
        self._stopped = False
        self._dirty = False

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

    # -------------------------------------------------------------- lifecycle
    async def async_load(self) -> None:
        await self.store.async_load(self.config)
        data = await self._state_store.async_load() or {}
        rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
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
        self._last_mood = self.mood_status().mood
        for area_id in self.config.enabled_rooms:
            result = self.coordinator.occupancy.result(area_id)
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            if result is not None:
                rt.occupancy_state, rt.occupied = result.state.value, result.occupied
                rt.visit_confirmed = bool(result.occupied) and result.state != OccupancyState.POSSIBLE_OCCUPANCY
        self._automations = None
        self._conflicts = None
        _LOGGER.debug(
            "lighting_start mood=%s active_rooms=%s", self._last_mood, self.lighting.active_rooms
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
        self._resubscribe()
        self._notify()

    def _invalidate_managed(self) -> None:
        self._managed = None
        self._conflicts = None
        self._resubscribe()

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
            if name.casefold() != auto_option:
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

    def plan(self, area_id: str, trigger: str = "preview", mood: str | None = None,
             config: LightingConfig | None = None) -> Plan:
        """Plan for a room. Never sends anything."""
        lighting = config or self.lighting
        status = self.mood_status()
        if mood is None:
            mood, _ = self.room_mood(area_id, status)
        result = self.coordinator.occupancy.result(area_id)
        note = None if mood else status.detail
        return build_plan(
            area_id=area_id,
            mood=mood,
            targets=self._targets(area_id),
            resolve_fn=lambda t: resolve(lighting, mood or "", area_id, t.entity_id, t.targets),
            occupancy_state=result.state.value if result else None,
            occupied=result.occupied if result else None,
            trigger=trigger,
            control_enabled=self.room_active(area_id),
            note=note,
        )

    # ------------------------------------------------------------- triggers
    @callback
    def _schedule_apply(self, area_id: str, trigger: str, only: set[str] | None = None) -> None:
        """Coalesce triggers per room; apply after a short delay."""
        if not self._running or self._stopped or not self.room_active(area_id):
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
                                        "profile_change", "reconnected") if t in triggers), sorted(triggers)[0])
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
            rt.occupancy_state, rt.occupied = result.state.value, result.occupied
            if not self._running:
                return
            if result.state == OccupancyState.CONFIRMED_OCCUPANCY and not rt.visit_confirmed:
                rt.visit_confirmed = True
                _LOGGER.debug("lighting_visit_confirmed area=%s", area_id)
                self._schedule_apply(area_id, "occupancy_confirmed")
            elif result.state == OccupancyState.VACANT and result.occupied is False:
                if rt.visit_confirmed:
                    rt.visit_confirmed = False
                if rt.overrides:
                    _LOGGER.debug("lighting_overrides_cleared area=%s reason=vacant", area_id)
                    rt.overrides.clear()
                    self._mark_dirty()
                    self._schedule_override_expiry()
            if changed:
                self._notify()

        return update

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
        if status.mood == self._last_mood:
            return
        old, self._last_mood = self._last_mood, status.mood
        _LOGGER.debug("lighting_mood_changed old=%s new=%s source=%s", old, status.mood, status.source)
        if status.mood is not None:
            for area_id in self.lighting.active_rooms:
                if not self.lighting.room(area_id).mood_override:
                    self._schedule_apply(area_id, "mood_change")
        self._notify()

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
        for store in (self._switch_contexts, self._own_contexts):
            for key in [k for k, t in store.items() if t < horizon]:
                del store[key]
        for key in [k for k, (until, _) in self._expected.items() if until < now]:
            del self._expected[key]

    def _classify(self, area_id: str, ctx: Context, now: float, echo: bool) -> str:
        """Who changed a light: ui, wall_switch, ours (echo), automation or unattributed."""
        if ctx.user_id:
            return "ui"
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
        expected = self._expected.get(entity_id)
        # Our command reported back with a new context (e.g. MQTT): same direction, shortly after.
        echo = bool(expected and expected[0] >= now and (expected[1] == "turn_off") == (new.state == "off"))
        for area_id, target in self._light_targets.get(entity_id, ()):
            source = self._classify(area_id, ctx, now, echo)
            if source == "ours":
                continue
            rt = self.runtime.setdefault(area_id, RoomRuntime())
            rt.last_commands.pop(target, None)
            rt.last_external[target] = {
                "at": _iso(dt_util.utcnow()), "source": source, "state": new.state, "entity_id": entity_id,
            }
            manual = source in ("ui", "wall_switch") or (
                source == "unattributed" and self.lighting.unattributed == UnattributedPolicy.MANUAL
            )
            if manual and self.room_active(area_id):
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
            for entity_id in [e for e, o in rt.overrides.items()
                              if o.get("until") and (dt_util.parse_datetime(o["until"]) or now) <= now]:
                del rt.overrides[entity_id]
                _LOGGER.debug("lighting_override_expired area=%s light=%s", area_id, entity_id)
                changed = True
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
            n = len(rt.overrides)
            rt.overrides.clear()
        else:
            n = 1 if rt.overrides.pop(entity_id, None) else 0
        if n:
            self._mark_dirty()
            self._schedule_override_expiry()
            self._notify()
        return n

    # ------------------------------------------------------------ execution
    async def async_apply(self, area_id: str, trigger: str, *, only: set[str] | None = None) -> Plan | None:
        """Plan and, in an activated room, execute. Records the outcome."""
        if self._stopped or not self.room_active(area_id):
            return None
        lock = self._locks.setdefault(area_id, asyncio.Lock())
        async with lock:
            plan = self.plan(area_id, trigger)
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
                self._finish(area_id, plan)
                return plan
            if not changes:
                rt.last_result = "no_change"
                rt.last_reason = _summary(plan.mood, relevant, trigger)
                rt.last_errors = []
                self._finish(area_id, plan)
                return plan
            errors = await self._execute(area_id, changes)
            ok = [i for i in changes if i.entity_id not in errors]
            for item in ok:
                rt.last_commands[item.entity_id] = (item.service or "", dict(item.data))
            rt.last_errors = [f"{e}: {msg}" for e, msg in errors.items()]
            rt.last_result = "applied" if not errors else ("partial" if ok else "failed")
            rt.last_reason = _summary(plan.mood, relevant, trigger, failed=len(errors))
            if ok:
                rt.last_applied_mood = plan.mood
                rt.last_applied_at = _iso(dt_util.utcnow())
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

    async def _execute(self, area_id: str, items) -> dict[str, str]:
        """Send the commands, batched by identical service data. Returns {entity: error}."""
        assert_room_control_permitted(self.room_active(area_id), area_id)
        batches: dict[tuple[str, str], list[str]] = {}
        for item in items:
            key = (item.service, json.dumps(item.data, sort_keys=True))
            batches.setdefault(key, []).append(item.entity_id)
        context = Context()
        now = time.monotonic()
        self._own_contexts[context.id] = now
        errors: dict[str, str] = {}
        for (service, data_json), entity_ids in batches.items():
            for entity_id in entity_ids:
                self._expected[entity_id] = (now + OWN_CHANGE_WINDOW_SECONDS, service)
            data = {**json.loads(data_json), "entity_id": entity_ids}
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
        self.coordinator.async_schedule_refresh()
        if self._running and (old.unrepresentable != new.unrepresentable):
            # The handling of a mood Main Mood cannot represent changes the effective mood.
            self._on_mood_event()
        if self._running:
            for area_id in self.config.enabled_rooms:
                o, n = old.room(area_id), new.room(area_id)
                if not n.control_enabled:
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
            "last_external_changes": rt.last_external,
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
    def _mark_dirty(self) -> None:
        self._dirty = True
        self._state_store.async_delay_save(self._state_data, LIGHTING_STATE_SAVE_DELAY_SECONDS)

    def _state_data(self) -> dict[str, Any]:
        self._dirty = False
        return {"rooms": {a: rt.persist() for a, rt in self.runtime.items()}}

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mood": self.mood_status().as_dict(),
            "lighting_config": self.lighting.as_dict(),
            "rooms": {a: self.room_diagnostics(a) for a in sorted(self.config.enabled_rooms)},
            "tracked_entities": list(self._tracked),
        }

    def with_room(self, area_id: str, **changes: Any) -> LightingConfig:
        return self.lighting.with_room(area_id, replace(self.lighting.room(area_id), **changes))


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
