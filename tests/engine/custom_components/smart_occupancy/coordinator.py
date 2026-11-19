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

from . import mood as mood_mod
from .const import (
    DOMAIN,
    LAST_CHECKED_REFRESH_MINUTES,
    REGISTRY_DEBOUNCE_SECONDS,
    STARTUP_GRACE_SECONDS,
    STATE_DEBOUNCE_SECONDS,
    Severity,
)
from .controller import LightingController
from .lighting_bridge import LightingBridge, LightingView
from .models import IntegrationConfig
from .occupancy import CONFIGURATION_INVALID, OPERATIONAL_UNAVAILABLE
from .curtain_bridge import CurtainBridge
from .curtains.manager import CurtainManager
from .forecast import ForecastManager
from .forecast_bridge import ForecastBridge
from .occupancy_manager import OccupancyManager
from .presence_log import record_config_change, record_transition
from .problems import ProblemTracker
from .snapshot import (  # noqa: F401 - re-exported (the entities and tests import them from here)
    CONFIG_ERROR,
    HEALTHY,
    STATUS_OPTIONS,
    UNAVAILABLE,
    WARNING,
    CategorySnapshot,
    EntityStatus,
    RoomSnapshot,
    Snapshot,
    build_snapshot,
    diagnostics_relevant,
    worst,
)

_LOGGER = logging.getLogger(__name__)


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
        self.occupancy = OccupancyManager(hass, self.config)
        self.occupancy.on_transition = self._log_transition
        self.lighting = LightingController(hass, self)
        # Phase A4: who controls lighting (this controller or the Smart Lighting App's), and what entities show.
        self.lighting_bridge = LightingBridge(hass, self)
        self.lighting_view = LightingView(self)
        # Occupancy forecast (0.5.0): observation only, it never controls anything.
        self.forecast = ForecastManager(hass, self)
        # Curtains: windows, presets, confirmed manual control (0.6.1) and per-window Automatic (0.6.2).
        # Occupancy is only read (for the "until the room has been empty" manual hold).
        self.curtains = CurtainManager(hass, self.occupancy)
        # Phase A5: who produces the forecasts and who commands the covers (this integration or the App).
        self.forecast_bridge = ForecastBridge(hass, self)
        self.curtain_bridge = CurtainBridge(hass, self)
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
            function=self._async_registry_refresh,
        )
        self._reported: set[str] = set()
        # Options written by async_apply_presence (already applied; the update listener must not reload).
        self._live_options: dict[str, Any] | None = None
        self._live_pending = 0
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

    async def _async_registry_refresh(self) -> None:
        # Registries changed: the cached light index and managed lights are stale.
        self.lighting.async_registry_changed()
        await self.async_refresh()

    @callback
    def async_schedule_refresh(self) -> None:
        """Request a (debounced) re-evaluation, e.g. after a lighting change."""
        if self.hass.is_stopping:
            return
        self.hass.async_create_task(self.async_request_refresh(), eager_start=True)

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

    @callback
    def _log_transition(self, area_id: str, previous, result, now: datetime) -> None:
        if previous is not None:  # the first evaluation after a start is the restored state, not a decision
            record_transition(self.hass, area_id, previous, result, now)

    @callback
    def async_apply_presence(self, config: IntegrationConfig, options: dict[str, Any] | None = None) -> bool:
        """Save changed presence settings and apply them at once, without reloading (0.3.8).

        Only for signals that are already assigned (enabled / excluded) and
        timing values: the occupancy engines keep every signal's value, so the
        rooms are re-evaluated with the new settings, no restart hold applies
        and no command is sent by the change itself. Returns False when the
        change needs a reload (other rooms or signals), which then happens
        through the options update listener as before.
        """
        if not self.occupancy.same_signals(config):
            return False
        # ``options``: the exact options to store (0.6.10: keeps keys the typed configuration does not know).
        options = dict(options) if options is not None else config.as_dict()
        record_config_change(self.hass, self.config, config, applied="live")
        self.config = config
        self.occupancy.async_reconfigure(config)
        # The update listener may run during async_update_entry (eagerly): count it before.
        self._live_options = options
        self._live_pending += 1
        if not self.hass.config_entries.async_update_entry(self.config_entry, options=options):
            self._live_pending -= 1  # nothing changed: no listener call to absorb
        self.lighting.async_presence_changed()
        self.lighting_bridge.options_changed(options)
        self.async_schedule_refresh()
        _LOGGER.info("presence_settings_applied rooms=%d (no reload)", len(config.enabled_rooms))
        return True

    def consume_live_options(self, options: dict[str, Any]) -> bool:
        """The options update listener: was this update applied live already?"""
        if self._live_pending and self._live_options == dict(options):
            self._live_pending -= 1
            return True
        return False

    async def async_recheck(self) -> None:
        """User-requested re-evaluation; always republishes entity states."""
        await self.async_refresh()
        self.async_update_listeners()

    # ---------------------------------------------------------------- compute
    async def _async_update_data(self) -> Snapshot:
        return self._build_snapshot()

    def _build_snapshot(self) -> Snapshot:
        now = dt_util.utcnow()
        observation = mood_mod.observe(self.hass, self.config.mood)
        mismatch = self._evaluate_mismatch(observation)
        settled = self._settled
        # Problems are needed before "first detected" can be tracked: build once without it, then stamp.
        draft = build_snapshot(self.hass, self.config, settled=settled, lighting=self.lighting,
                               observation=observation, mismatch=mismatch, first_detected={}, now=now,
                               checked_bucket=None)
        first_detected = self.tracker.update(draft.problems, now, prune=settled)
        bucket = None
        if draft.status != HEALTHY:
            minute = (now.minute // LAST_CHECKED_REFRESH_MINUTES) * LAST_CHECKED_REFRESH_MINUTES
            bucket = now.replace(minute=minute, second=0, microsecond=0).isoformat()
        snapshot = Snapshot(
            status=draft.status, configured_mode=draft.configured_mode, effective_mode=draft.effective_mode,
            mood=draft.mood, mood_mismatch=draft.mood_mismatch, rooms=draft.rooms, categories=draft.categories,
            problems=draft.problems, discovered_signals=draft.discovered_signals, settled=settled,
            checked_bucket=bucket, evaluated_at=now, first_detected=first_detected,
            mood_lighting_rooms=draft.mood_lighting_rooms,
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
