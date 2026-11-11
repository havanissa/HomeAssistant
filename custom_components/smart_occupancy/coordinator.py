"""Observe-mode runtime.

Event driven: recomputes only when a configured entity changes state or a
relevant registry (areas, devices, entities, labels) changes. There is no
polling interval. Bursts of events are coalesced by a debouncer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
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
    REGISTRY_DEBOUNCE_SECONDS,
    STATE_DEBOUNCE_SECONDS,
    OperatingMode,
    Severity,
)
from .models import IntegrationConfig
from .occupancy import AVAILABLE, MISSING, SignalHealth, entity_health, signal_health
from .prediction import NullPredictor
from .supervision import effective_mode
from .validation import Problem, validate

_LOGGER = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

ROOM_OK = "ok"
ROOM_DEGRADED = "degraded"
ROOM_NOT_CONFIGURED = "not_configured"
ROOM_AREA_MISSING = "area_missing"


@dataclass(slots=True, frozen=True)
class RoomSnapshot:
    area_id: str
    name: str
    status: str
    signals: tuple[SignalHealth, ...]
    lights: tuple[tuple[str, str, tuple[str, ...]], ...]  # (entity, health, categories)
    reference_occupancy: tuple[str, str] | None
    problem_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "status": self.status,
            "signals": [s.as_dict() for s in self.signals],
            "lights": [
                {"entity_id": e, "health": h, "categories": list(c)}
                for e, h, c in self.lights
            ],
            "reference_occupancy": (
                {"entity_id": self.reference_occupancy[0], "health": self.reference_occupancy[1]}
                if self.reference_occupancy
                else None
            ),
            "problem_count": self.problem_count,
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
    status: str
    configured_mode: OperatingMode
    effective_mode: OperatingMode
    mood: mood_mod.MoodObservation
    mood_mismatch: bool
    rooms: tuple[RoomSnapshot, ...]
    categories: tuple[CategorySnapshot, ...]
    problems: tuple[Problem, ...]
    missing_entities: tuple[str, ...]
    unavailable_entities: tuple[str, ...]
    discovered_signals: tuple[tuple[str, str, str], ...]  # (area_id, entity_id, kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "configured_mode": self.configured_mode.value,
            "effective_mode": self.effective_mode.value,
            "mood": self.mood.as_dict(),
            "mood_mismatch": self.mood_mismatch,
            "rooms": [r.as_dict() for r in self.rooms],
            "categories": [c.as_dict() for c in self.categories],
            "problems": [p.as_dict() for p in self.problems],
            "missing_entities": list(self.missing_entities),
            "unavailable_entities": list(self.unavailable_entities),
            "discovered_signals": [
                {"area_id": a, "entity_id": e, "suggested_kind": k}
                for a, e, k in self.discovered_signals
            ],
        }


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
        self._unsubs: list[CALLBACK_TYPE] = []
        self._started_unsub: CALLBACK_TYPE | None = None
        self._mismatch_since: datetime | None = None
        self._mismatch_timer: CALLBACK_TYPE | None = None
        self._registry_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=REGISTRY_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_refresh,
        )
        self._reported: set[tuple[str, str, str | None, str | None]] = set()
        self._started = hass.state is CoreState.running

    # ------------------------------------------------------------------ setup
    @property
    def tracked_entities(self) -> list[str]:
        ids = set(self.config.mood.entity_ids)
        for room in self.config.enabled_rooms.values():
            ids.update(room.referenced_entities)
        return sorted(ids)

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
            "observe_start mode=%s tracked_entities=%d rooms=%d",
            self.config.operating_mode, len(tracked), len(self.config.enabled_rooms),
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._started_unsub:
            self._started_unsub()
            self._started_unsub = None
        if self._mismatch_timer:
            self._mismatch_timer()
            self._mismatch_timer = None
        self._registry_debouncer.async_shutdown()

    @callback
    def _on_state_event(self, event: Event) -> None:
        self.hass.async_create_task(self.async_request_refresh(), eager_start=True)

    @callback
    def _on_registry_event(self, event: Event) -> None:
        self.hass.async_create_task(self._registry_debouncer.async_call(), eager_start=True)

    async def _on_started(self, event: Event) -> None:
        self._started_unsub = None  # one-shot listener is already removed
        self._started = True
        # Log every problem that is still present once start-up has settled.
        self._reported = set()
        await self.async_refresh()
        data = self.data
        _LOGGER.info(
            "observe_summary status=%s rooms=%d problems=%d missing=%d unavailable=%d",
            data.status, len(data.rooms), len(data.problems),
            len(data.missing_entities), len(data.unavailable_entities),
        )

    # ---------------------------------------------------------------- compute
    async def _async_update_data(self) -> Snapshot:
        return self._build_snapshot()

    def _build_snapshot(self) -> Snapshot:
        hass = self.hass
        config = self.config
        problems = validate(hass, config)

        observation = mood_mod.observe(hass, config.mood)
        mismatch = self._evaluate_mismatch(observation)

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

        missing: set[str] = set(observation.missing) - {
            m for m in observation.missing if m.startswith("<")
        }
        unavailable: set[str] = set(observation.unavailable)
        rooms: list[RoomSnapshot] = []
        discovered: list[tuple[str, str, str]] = []
        for area_id, room in sorted(config.enabled_rooms.items()):
            name = discovery.area_name(hass, area_id)
            signals = tuple(signal_health(hass, room))
            lights = tuple(
                (e, entity_health(hass, e), tuple(by_light.get(e, []))) for e in room.lights
            )
            ref = (
                (room.reference_occupancy_entity, entity_health(hass, room.reference_occupancy_entity))
                if room.reference_occupancy_entity
                else None
            )
            healths = [s.health for s in signals] + [h for _, h, _ in lights]
            if ref:
                healths.append(ref[1])
            for entity_id, health in (
                [(s.entity_id, s.health) for s in signals]
                + [(e, h) for e, h, _ in lights]
                + ([ref] if ref else [])
            ):
                if health == MISSING:
                    missing.add(entity_id)
                elif health != AVAILABLE:
                    unavailable.add(entity_id)

            if name is None:
                status = ROOM_AREA_MISSING
            elif not room.sensors and not room.lights:
                status = ROOM_NOT_CONFIGURED
            elif any(h != AVAILABLE for h in healths):
                status = ROOM_DEGRADED
            else:
                status = ROOM_OK
            rooms.append(
                RoomSnapshot(
                    area_id=area_id,
                    name=name or area_id,
                    status=status,
                    signals=signals,
                    lights=lights,
                    reference_occupancy=ref,
                    problem_count=sum(1 for p in problems if p.area_id == area_id),
                )
            )
            if name is not None:
                found = discovery.discover_area(hass, area_id)
                discovered.extend(
                    (area_id, e.entity_id, e.suggested_kind.value)
                    for e in found.signals
                    if e.suggested_kind
                )

        if mismatch:
            problems.append(
                Problem(
                    Severity.WARNING,
                    "mood_mismatch",
                    f"Main Mood is '{observation.main_mood}' but the existing rule "
                    f"expects '{observation.expected_main_mood}' "
                    f"(source: {observation.source}).",
                    entity_id=config.mood.main_mood_entity,
                )
            )

        if any(p.severity == Severity.ERROR for p in problems):
            status = STATUS_ERROR
        elif any(p.severity == Severity.WARNING for p in problems) or unavailable:
            status = STATUS_WARNING
        else:
            status = STATUS_OK

        snapshot = Snapshot(
            status=status,
            configured_mode=config.operating_mode,
            effective_mode=effective_mode(config.operating_mode),
            mood=observation,
            mood_mismatch=mismatch,
            rooms=tuple(rooms),
            categories=categories,
            problems=tuple(problems),
            missing_entities=tuple(sorted(missing)),
            unavailable_entities=tuple(sorted(unavailable)),
            discovered_signals=tuple(discovered),
        )
        self._log_transitions(snapshot)
        return snapshot

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
        current = {(p.code, p.message, p.area_id, p.entity_id): p for p in snapshot.problems}
        if not self._started:
            # During startup integrations are still loading; stay quiet.
            self._reported = set(current)
            return
        for key in current.keys() - self._reported:
            p = current[key]
            level = logging.DEBUG if p.severity == Severity.INFO else logging.WARNING
            _LOGGER.log(
                level, "problem_detected severity=%s code=%s area=%s entity=%s message=%s",
                p.severity, p.code, p.area_id, p.entity_id, p.message,
            )
        for key in self._reported - current.keys():
            _LOGGER.info(
                "problem_resolved code=%s area=%s entity=%s", key[0], key[2], key[3]
            )
        self._reported = set(current)
        _LOGGER.debug(
            "observe_snapshot status=%s mood_auto=%s mood_manual=%s mood_main=%s "
            "mood_expected=%s consistent=%s mismatch=%s",
            snapshot.status, snapshot.mood.auto_mood, snapshot.mood.manual_mood,
            snapshot.mood.main_mood, snapshot.mood.expected_main_mood,
            snapshot.mood.consistent, snapshot.mood_mismatch,
        )
