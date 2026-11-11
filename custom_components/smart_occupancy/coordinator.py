"""Observe-mode runtime.

Event driven: recomputes only when a configured entity changes in a way that
matters for diagnostics (availability, name, mood value, light capabilities)
or a relevant registry (areas, devices, entities, labels) changes, when the
integration (re)loads, and once Home Assistant has finished starting. There
is no polling interval. Bursts of events are coalesced by a debouncer.

Plain on/off changes of occupancy signals are handled by the occupancy
engine (occupancy_manager.py), not here, so motion does not rebuild the
diagnostics snapshot.

Every evaluation is complete: the snapshot is rebuilt from scratch, so a
problem disappears as soon as its cause is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import discovery, lighting, mood as mood_mod
from .const import (
    DOMAIN,
    LAST_CHECKED_REFRESH_MINUTES,
    REGISTRY_DEBOUNCE_SECONDS,
    STARTUP_GRACE_SECONDS,
    STATE_DEBOUNCE_SECONDS,
    Classification,
    OperatingMode,
    ProblemCategory,
    Severity,
)
from .models import IntegrationConfig
from .occupancy import (
    AVAILABLE,
    CONFIGURATION_INVALID,
    OPERATIONAL_UNAVAILABLE,
    PENDING,
    SignalHealth,
    entity_health,
    friendly_name,
    signal_health,
)
from .occupancy_manager import OccupancyManager
from .prediction import NullPredictor
from .problems import ProblemTracker
from .supervision import effective_mode
from .validation import ENTITY_PROBLEM_CODES, Problem, validate

_LOGGER = logging.getLogger(__name__)

HEALTHY = Classification.HEALTHY
WARNING = Classification.WARNING
CONFIG_ERROR = Classification.CONFIG_ERROR
UNAVAILABLE = Classification.UNAVAILABLE
STATUS_OPTIONS = [HEALTHY.value, WARNING.value, CONFIG_ERROR.value, UNAVAILABLE.value]

# Light attributes the diagnostics depend on (group membership, capabilities).
_LIGHT_ATTRIBUTES = ("entity_id", "supported_color_modes", "effect_list", "is_hue_group")


def _availability(state: State) -> tuple[str, bool]:
    value = state.state if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) else "known"
    return value, bool(state.attributes.get("restored"))


def diagnostics_relevant(old: State | None, new: State | None, *, value_matters: bool) -> bool:
    """Does this state change affect the diagnostics snapshot?"""
    if old is None or new is None or value_matters:
        return True
    if _availability(old) != _availability(new):
        return True
    if old.attributes.get("friendly_name") != new.attributes.get("friendly_name"):
        return True
    if new.domain == "light":
        return any(old.attributes.get(k) != new.attributes.get(k) for k in _LIGHT_ATTRIBUTES)
    return False


@dataclass(slots=True, frozen=True)
class EntityStatus:
    entity_id: str
    name: str | None
    health: str
    detail: str  # sensor kind/role, or light categories

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "health": self.health, "detail": self.detail}


@dataclass(slots=True, frozen=True)
class RoomSnapshot:
    area_id: str
    name: str
    status: Classification
    usable: bool
    configuration_valid: bool
    signals: tuple[SignalHealth, ...]
    sensors: tuple[EntityStatus, ...]
    lights: tuple[EntityStatus, ...]
    reference_occupancy: EntityStatus | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "status": self.status.value,
            "usable": self.usable,
            "configuration_valid": self.configuration_valid,
            "sensors": [s.as_dict() for s in self.sensors],
            "lights": [s.as_dict() for s in self.lights],
            "reference_occupancy": self.reference_occupancy.as_dict() if self.reference_occupancy else None,
            "reasons": list(self.reasons),
        }


@dataclass(slots=True, frozen=True)
class CategorySnapshot:
    key: str
    name: str
    label_id: str | None
    label_name: str | None
    lights: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "label_id": self.label_id,
            "label_name": self.label_name,
            "lights": list(self.lights),
        }


@dataclass(slots=True, frozen=True)
class Snapshot:
    status: Classification
    configured_mode: OperatingMode
    effective_mode: OperatingMode
    mood: mood_mod.MoodObservation
    mood_mismatch: bool
    rooms: tuple[RoomSnapshot, ...]
    categories: tuple[CategorySnapshot, ...]
    problems: tuple[Problem, ...]
    discovered_signals: tuple[tuple[str, str, str], ...]  # (area_id, entity_id, kind)
    settled: bool
    # Bumps every LAST_CHECKED_REFRESH_MINUTES while problems exist, so the
    # "last checked" attributes are republished at a bounded rate.
    checked_bucket: str | None
    evaluated_at: datetime = field(compare=False)
    first_detected: dict[str, str] = field(compare=False, default_factory=dict)

    @property
    def last_checked(self) -> str:
        return self.evaluated_at.isoformat(timespec="seconds")

    def problem_dict(self, p: Problem) -> dict[str, Any]:
        return p.as_dict(self.first_detected.get(p.key), self.last_checked)

    def problems_where(self, *, category: ProblemCategory | None = None, area_id: str | None = None,
                       include_info: bool = False) -> list[Problem]:
        return [
            p for p in self.problems
            if (include_info or p.severity != Severity.INFO)
            and (category is None or p.category == category)
            and (area_id is None or p.area_id == area_id)
        ]

    def count(self, classification: Classification) -> int:
        return sum(1 for p in self.problems if p.classification == classification)

    def entity_issues(self, healths: frozenset[str]) -> list[dict[str, Any]]:
        """One entry per affected entity (an entity can be in several rooms)."""
        out: dict[str, dict[str, Any]] = {}
        for p in self.problems:
            if p.code not in ENTITY_PROBLEM_CODES or p.health not in healths or not p.entity_id:
                continue
            item = out.get(p.entity_id)
            if item is None:
                item = out[p.entity_id] = {
                    "entity_id": p.entity_id,
                    "name": p.name,
                    "rooms": [],
                    **{k: v for k, v in self.problem_dict(p).items()
                       if k not in ("entity_id", "name", "room", "area_id")},
                }
            if p.room and p.room not in item["rooms"]:
                item["rooms"].append(p.room)
        return sorted(out.values(), key=lambda i: i["entity_id"])

    @staticmethod
    def issue_line(item: dict[str, Any]) -> str:
        name = f" ({item['name']})" if item.get("name") else ""
        rooms = ", ".join(item["rooms"]) or "Global"
        return f"{item['entity_id']}{name} — {rooms} — {item['title']}"

    def summary(self) -> str:
        if self.status == HEALTHY:
            return "No problems."
        parts = []
        for cls, word in ((CONFIG_ERROR, "configuration error"), (UNAVAILABLE, "unavailable"),
                          (WARNING, "warning")):
            if n := self.count(cls):
                parts.append(f"{n} {word}{'s' if n != 1 and cls != UNAVAILABLE else ''}")
        return ", ".join(parts) + "."

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "settled": self.settled,
            "last_checked": self.last_checked,
            "configured_mode": self.configured_mode.value,
            "effective_mode": self.effective_mode.value,
            "mood": self.mood.as_dict(),
            "mood_mismatch": self.mood_mismatch,
            "rooms": [r.as_dict() for r in self.rooms],
            "categories": [c.as_dict() for c in self.categories],
            "problems": [self.problem_dict(p) for p in self.problems],
            "discovered_signals": [
                {"area_id": a, "entity_id": e, "suggested_kind": k}
                for a, e, k in self.discovered_signals
            ],
        }


def worst(statuses: list[Classification]) -> Classification:
    for status in (CONFIG_ERROR, UNAVAILABLE, WARNING):
        if status in statuses:
            return status
    return HEALTHY


type SmartOccupancyConfigEntry = ConfigEntry[SmartOccupancyCoordinator]


class SmartOccupancyCoordinator(DataUpdateCoordinator[Snapshot]):
    """Builds observation snapshots in response to events."""

    config_entry: SmartOccupancyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,  # event driven, never polls
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=STATE_DEBOUNCE_SECONDS, immediate=True
            ),
            always_update=False,
        )
        self.config = IntegrationConfig.from_dict(dict(entry.options))
        self.predictor = NullPredictor()
        self.occupancy = OccupancyManager(hass, self.config)
        self.tracker = ProblemTracker(hass)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._started_unsub: CALLBACK_TYPE | None = None
        self._settle_timer: CALLBACK_TYPE | None = None
        self._mismatch_since: datetime | None = None
        self._mismatch_timer: CALLBACK_TYPE | None = None
        self._registry_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=REGISTRY_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_refresh,
        )
        self._reported: set[str] = set()
        # A reload while Home Assistant is running needs no start-up grace.
        self._started = hass.state is CoreState.running
        self._settled = self._started

    # ------------------------------------------------------------------ setup
    @property
    def tracked_entities(self) -> list[str]:
        ids = set(self.config.mood.entity_ids)
        for room in self.config.enabled_rooms.values():
            ids.update(room.referenced_entities)
        return sorted(ids)

    @property
    def settled(self) -> bool:
        return self._settled

    @callback
    def async_start(self) -> None:
        """Subscribe to the events that can change a snapshot."""
        if tracked := self.tracked_entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, tracked, self._on_state_event)
            )
        for event_type in (
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            ar.EVENT_AREA_REGISTRY_UPDATED,
            lr.EVENT_LABEL_REGISTRY_UPDATED,
        ):
            self._unsubs.append(self.hass.bus.async_listen(event_type, self._on_registry_event))
        if not self._started:
            self._started_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_started
            )
        _LOGGER.debug(
            "observe_start mode=%s tracked_entities=%d rooms=%d settled=%s",
            self.config.operating_mode, len(tracked), len(self.config.enabled_rooms), self._settled,
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for attr in ("_started_unsub", "_settle_timer", "_mismatch_timer"):
            if unsub := getattr(self, attr):
                unsub()
                setattr(self, attr, None)
        self._registry_debouncer.async_shutdown()

    @callback
    def _on_state_event(self, event: Event) -> None:
        data = event.data
        if not diagnostics_relevant(
            data["old_state"], data["new_state"],
            value_matters=data["entity_id"] in self.config.mood.entity_ids,
        ):
            return
        self.hass.async_create_task(self.async_request_refresh(), eager_start=True)

    @callback
    def _on_registry_event(self, event: Event) -> None:
        self.hass.async_create_task(self._registry_debouncer.async_call(), eager_start=True)

    async def _on_started(self, event: Event) -> None:
        self._started_unsub = None  # one-shot listener is already removed
        self._started = True
        # Give slow integrations time to provide their entities before
        # reporting them as not provided. One timer, not polling.
        self._settle_timer = async_call_later(self.hass, STARTUP_GRACE_SECONDS, self._on_settled)
        await self.async_refresh()

    @callback
    def _on_settled(self, _now: datetime) -> None:
        self._settle_timer = None
        self._settled = True
        self._reported = set()  # log every remaining problem once
        self.hass.async_create_task(self._async_settled_refresh(), eager_start=True)

    async def _async_settled_refresh(self) -> None:
        await self.async_refresh()
        data = self.data
        _LOGGER.info(
            "observe_summary status=%s rooms=%d config_errors=%d unavailable=%d warnings=%d",
            data.status, len(data.rooms), data.count(CONFIG_ERROR), data.count(UNAVAILABLE),
            data.count(WARNING),
        )

    async def async_recheck(self) -> None:
        """User-requested re-evaluation; always republishes entity states."""
        await self.async_refresh()
        self.async_update_listeners()

    # ---------------------------------------------------------------- compute
    async def _async_update_data(self) -> Snapshot:
        return self._build_snapshot()

    def _build_snapshot(self) -> Snapshot:
        hass = self.hass
        config = self.config
        now = dt_util.utcnow()
        settled = self._settled
        problems = validate(hass, config, settled=settled)

        observation = mood_mod.observe(hass, config.mood)
        mismatch = self._evaluate_mismatch(observation)
        if mismatch:
            problems.append(Problem(
                "mood_mismatch", Severity.WARNING, ProblemCategory.OPERATIONAL, "Main Mood does not follow the rule",
                f"Main Mood is '{observation.main_mood}', but the existing rule expects "
                f"'{observation.expected_main_mood}' (it should follow "
                f"{'Auto Mood' if observation.source == 'auto' else 'Manual Mood'}). This clears as soon as "
                "Main Mood matches again.",
                "Check the automation that sets Main Mood from Auto/Manual Mood, and its trace.",
                entity_id=config.mood.main_mood_entity,
                name=friendly_name(hass, config.mood.main_mood_entity) if config.mood.main_mood_entity else None,
                subject="mood",
            ))

        by_category, by_light = lighting.classify_lights(hass, config.categories)
        label_reg = lr.async_get(hass)
        categories = tuple(
            CategorySnapshot(
                key=c.key,
                name=c.name,
                label_id=c.label_id,
                label_name=(
                    label.name
                    if c.label_id and (label := label_reg.async_get_label(c.label_id))
                    else None
                ),
                lights=tuple(by_category.get(c.key, [])),
            )
            for c in config.categories
        )

        rooms: list[RoomSnapshot] = []
        discovered: list[tuple[str, str, str]] = []
        for area_id, room in sorted(config.enabled_rooms.items()):
            rooms.append(self._room_snapshot(area_id, room, problems, by_light, settled))
            if discovery.area_name(hass, area_id) is not None:
                found = discovery.discover_area(hass, area_id)
                discovered.extend(
                    (area_id, e.entity_id, e.suggested_kind.value) for e in found.signals if e.suggested_kind
                )

        statuses = [r.status for r in rooms]
        if any(p.classification == CONFIG_ERROR for p in problems):
            statuses.append(CONFIG_ERROR)
        if any(p.classification in (WARNING, UNAVAILABLE) for p in problems):
            statuses.append(WARNING)
        status = worst(statuses)

        first_detected = self.tracker.update(problems, now, prune=settled)
        bucket = None
        if status != HEALTHY:
            minute = (now.minute // LAST_CHECKED_REFRESH_MINUTES) * LAST_CHECKED_REFRESH_MINUTES
            bucket = now.replace(minute=minute, second=0, microsecond=0).isoformat()

        snapshot = Snapshot(
            status=status,
            configured_mode=config.operating_mode,
            effective_mode=effective_mode(config.operating_mode),
            mood=observation,
            mood_mismatch=mismatch,
            rooms=tuple(rooms),
            categories=categories,
            problems=tuple(problems),
            discovered_signals=tuple(discovered),
            settled=settled,
            checked_bucket=bucket,
            evaluated_at=now,
            first_detected=first_detected,
        )
        self._log_transitions(snapshot)
        return snapshot

    def _room_snapshot(self, area_id, room, problems: list[Problem], by_light, settled: bool) -> RoomSnapshot:
        hass = self.hass
        name = discovery.area_name(hass, area_id)
        signals = tuple(signal_health(hass, room, settled=settled))
        sensors = tuple(
            EntityStatus(s.entity_id, friendly_name(hass, s.entity_id), s.health, f"{s.kind}/{s.role}")
            for s in signals
        )
        lights = tuple(
            EntityStatus(e, friendly_name(hass, e), entity_health(hass, e, settled=settled),
                         ", ".join(by_light.get(e, [])) or "no category")
            for e in room.lights
        )
        ref = None
        if room.reference_occupancy_entity:
            e = room.reference_occupancy_entity
            ref = EntityStatus(e, friendly_name(hass, e), entity_health(hass, e, settled=settled), "reference")

        room_problems = [p for p in problems if p.area_id == area_id and p.severity != Severity.INFO]
        configuration_valid = not any(p.classification == CONFIG_ERROR for p in room_problems)
        essential = [s for s in signals if s.essential]
        usable = name is not None and any(s.health in (AVAILABLE, PENDING) for s in essential)

        if not configuration_valid:
            status = CONFIG_ERROR
        elif essential and not any(s.health in (AVAILABLE, PENDING) for s in essential):
            status = UNAVAILABLE
        elif room_problems:
            status = WARNING
        else:
            status = HEALTHY

        reasons = tuple(
            f"{p.title}: {p.entity_id}" + (f" ({p.name})" if p.name else "") if p.entity_id else p.title
            for p in room_problems
        )
        if any(h.health == PENDING for h in (*sensors, *lights)):
            reasons += ("Waiting for Home Assistant to finish starting",)
        return RoomSnapshot(
            area_id=area_id,
            name=name or area_id,
            status=status,
            usable=usable,
            configuration_valid=configuration_valid,
            signals=signals,
            sensors=sensors,
            lights=lights,
            reference_occupancy=ref,
            reasons=reasons,
        )

    def _evaluate_mismatch(self, observation: mood_mod.MoodObservation) -> bool:
        """Mismatch only counts once it outlives the grace period."""
        if observation.consistent is not False:
            self._mismatch_since = None
            if self._mismatch_timer:
                self._mismatch_timer()
                self._mismatch_timer = None
            return False
        now = dt_util.utcnow()
        grace = timedelta(seconds=self.config.mood.mismatch_grace_seconds)
        if self._mismatch_since is None:
            self._mismatch_since = now
        elapsed = now - self._mismatch_since
        if elapsed >= grace:
            return True
        if self._mismatch_timer is None:
            self._mismatch_timer = async_call_later(
                self.hass, (grace - elapsed).total_seconds() + 0.1, self._mismatch_timer_fired
            )
        return False

    @callback
    def _mismatch_timer_fired(self, _now: datetime) -> None:
        self._mismatch_timer = None
        self.hass.async_create_task(self.async_refresh(), eager_start=True)

    def _log_transitions(self, snapshot: Snapshot) -> None:
        """Log problems once when they appear and once when they clear."""
        current = {p.key: p for p in snapshot.problems}
        if not snapshot.settled:
            # During start-up integrations are still loading; stay quiet.
            self._reported = set(current)
            return
        for key in current.keys() - self._reported:
            p = current[key]
            level = logging.DEBUG if p.severity == Severity.INFO else logging.WARNING
            _LOGGER.log(
                level, "problem_detected classification=%s type=%s room=%s entity=%s explanation=%s",
                p.classification, p.code, p.room, p.entity_id, p.explanation,
            )
        for key in self._reported - current.keys():
            code, area_id, entity_id, _ = (key.split("|") + ["", "", "", ""])[:4]
            _LOGGER.info("problem_resolved type=%s area=%s entity=%s", code, area_id or None, entity_id or None)
        self._reported = set(current)
        _LOGGER.debug(
            "observe_snapshot status=%s mood_auto=%s mood_manual=%s mood_main=%s "
            "mood_expected=%s consistent=%s mismatch=%s problems=%d",
            snapshot.status, snapshot.mood.auto_mood, snapshot.mood.manual_mood,
            snapshot.mood.main_mood, snapshot.mood.expected_main_mood,
            snapshot.mood.consistent, snapshot.mood_mismatch, len(snapshot.problems),
        )


__all__ = [
    "CONFIGURATION_INVALID",
    "OPERATIONAL_UNAVAILABLE",
    "STATUS_OPTIONS",
    "SmartOccupancyConfigEntry",
    "SmartOccupancyCoordinator",
    "Snapshot",
]
