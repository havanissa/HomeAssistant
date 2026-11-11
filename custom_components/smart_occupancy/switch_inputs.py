"""Wall switch input paths: subscriptions, normalisation, learning (Phase 0.3.5).

Every input path delivers one normalised ``ButtonEvent`` to the switch
controller (``switch_control.py``):

* **zigbee2mqtt** - Home Assistant's MQTT integration (never a separate
  broker connection) subscribed to the configured switch topics only
  (``zigbee2mqtt/avb_ksu``; no wildcards). Retained messages, battery and
  link-quality updates and malformed payloads are ignored. The device's
  availability topic is read for diagnostics. Zigbee2MQTT's retained device
  list (``<base>/bridge/devices``) gives the device's declared actions, its
  bindings and renames (by IEEE address). Nothing is published.
* **device_trigger** - Home Assistant MQTT device triggers of the device,
  attached like an automation trigger. Messages replayed right after attaching
  are ignored.
* **event_entity** - an event entity. Its state is the time of the last
  event; restored states and stale events are not presses.

Only one path per switch is authoritative (preference: raw MQTT, device
triggers, event entity, unless chosen explicitly). While learning, every
available path listens and the deduplicator keeps one copy of each press.

Subscriptions follow the configuration: a removed switch or room is
unsubscribed, and everything is removed on unload. After an MQTT reconnect or
an MQTT integration reload the topics are subscribed again. A failure in one
switch never reaches the occupancy controller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    ATTACH_SETTLE_SECONDS,
    DEFAULT_LEARN_SECONDS,
    MAX_LEARN_SECONDS,
    STALE_EVENT_SECONDS,
    SWITCH_HISTORY_SIZE,
    SwitchSource,
)
from .switch_discovery import (
    Z2MDevice,
    async_device_triggers,
    event_entities,
    parse_bridge_devices,
    resolve_source,
)
from .switches import (
    IGNORED_WORDS,
    ButtonEvent,
    Deduplicator,
    SwitchConfig,
    action_label,
    event_entity_action,
    parse_mqtt_payload,
    validate_topic,
)

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

Key = tuple[str, str]  # (area id, switch id)


def _iso(value: datetime | None = None) -> str:
    return (value or dt_util.utcnow()).isoformat(timespec="seconds")


@dataclass(slots=True)
class SwitchStatus:
    """Runtime diagnostics of one switch (the last event and what happened)."""

    sources: list[str] = field(default_factory=list)     # paths listening now
    authoritative: str | None = None
    available_sources: list[str] = field(default_factory=list)
    topic: str | None = None
    availability: str | None = None                      # online / offline (Zigbee2MQTT) / unavailable
    problem: str | None = None                           # why the switch cannot be listened to
    last_event: dict[str, Any] | None = None
    last_result: str | None = None
    last_result_ok: bool | None = None
    last_command: str | None = None
    override: bool = False
    last_ignored: dict[str, Any] | None = None
    ignored_updates: int = 0                             # battery / link-quality messages (not shown)
    history: deque = field(default_factory=lambda: deque(maxlen=SWITCH_HISTORY_SIZE))

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources), "authoritative": self.authoritative,
            "available_sources": list(self.available_sources), "topic": self.topic,
            "availability": self.availability, "problem": self.problem, "last_event": self.last_event,
            "last_result": self.last_result, "last_result_ok": self.last_result_ok,
            "last_command": self.last_command, "override": self.override, "last_ignored": self.last_ignored,
            "ignored_updates": self.ignored_updates, "history": list(self.history),
        }

    def persist(self) -> dict[str, Any]:
        return {"last_event": self.last_event, "last_result": self.last_result,
                "last_result_ok": self.last_result_ok, "last_command": self.last_command,
                "override": self.override, "history": list(self.history)}

    def restore(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self.last_event = data.get("last_event") if isinstance(data.get("last_event"), dict) else None
        self.last_result = data.get("last_result")
        self.last_result_ok = data.get("last_result_ok")
        self.last_command = data.get("last_command")
        self.override = bool(data.get("override"))
        for item in data.get("history") or []:
            if isinstance(item, dict):
                self.history.append(item)


@dataclass(slots=True)
class LearnSession:
    until: datetime
    cancel: CALLBACK_TYPE
    captured: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"active": True, "until": _iso(self.until), "captured": list(self.captured)}


@dataclass(slots=True)
class _Active:
    """The subscriptions of one switch."""

    plan: dict[str, Any] = field(default_factory=dict)   # source -> what it listens to
    unsubs: dict[str, list[CALLBACK_TYPE]] = field(default_factory=dict)
    settle_until: dict[str, float] = field(default_factory=dict)


class SwitchInputs:
    """Owns every switch subscription and turns messages into button events."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController,
                 handler: Callable[[ButtonEvent], None]) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self._handler = handler
        self.dedupe = Deduplicator()
        self.status: dict[Key, SwitchStatus] = {}
        self.learning: dict[Key, LearnSession] = {}
        self.bridge_devices: dict[str, Z2MDevice] = {}
        self.coordinator: str | None = None
        self.bridge_seen_at: str | None = None
        self.mqtt_state = "not_configured"   # not_configured | waiting | connected | disconnected
        self._active: dict[Key, _Active] = {}
        self._triggers: dict[str, list[dict[str, Any]]] = {}
        self._triggers_known: set[str] = set()
        self._event_unsub: CALLBACK_TYPE | None = None
        self._event_entities: dict[str, list[Key]] = {}
        self._bridge_unsub: CALLBACK_TYPE | None = None
        self._bridge_topic: str | None = None
        self._bridge_wanted = False
        self._conn_unsub: CALLBACK_TYPE | None = None
        self._tasks: set[asyncio.Task] = set()
        self._apply_lock = asyncio.Lock()
        self._apply_again = False
        self._mqtt_ready = False
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------- lifecycle
    @property
    def mqtt_configured(self) -> bool:
        return bool(self.hass.config_entries.async_entries("mqtt"))

    @callback
    def async_start(self, restored: dict[str, Any] | None = None) -> None:
        self._started = True
        for area_id, room in self.ctrl.lighting.rooms.items():
            for switch in room.switches:
                self.status.setdefault((area_id, switch.id), SwitchStatus()).restore(
                    ((restored or {}).get(area_id) or {}).get(switch.id))
        if self.mqtt_configured:
            from homeassistant.components import mqtt

            self.mqtt_state = "waiting"
            self._conn_unsub = mqtt.async_subscribe_connection_status(self.hass, self._on_connection)
            self._create_task(self._async_wait_for_mqtt())
        self.async_config_changed()

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for session in self.learning.values():
            session.cancel()
        self.learning.clear()
        for key in list(self._active):
            self._unsubscribe_all(key)
        self._active.clear()
        if self._event_unsub:
            self._event_unsub()
            self._event_unsub = None
        if self._bridge_unsub:
            self._safe(self._bridge_unsub)
            self._bridge_unsub = None
        if self._conn_unsub:
            self._conn_unsub()
            self._conn_unsub = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    def persist(self) -> dict[str, Any]:
        out: dict[str, dict[str, Any]] = {}
        for (area_id, switch_id), status in self.status.items():
            if self.ctrl.lighting.room(area_id).switch(switch_id) is not None:
                out.setdefault(area_id, {})[switch_id] = status.persist()
        return out

    def _create_task(self, coro) -> None:
        if self._stopped:
            coro.close()
            return
        task = self.hass.async_create_background_task(coro, "smart_occupancy switch inputs")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_wait_for_mqtt(self) -> None:
        from homeassistant.components import mqtt

        try:
            ready = await mqtt.async_wait_for_mqtt_client(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("switch_mqtt_wait_failed error=%s", err)
            ready = False
        if self._stopped:
            return
        self._mqtt_ready = ready
        if ready:
            self.mqtt_state = "connected" if mqtt.is_connected(self.hass) else "disconnected"
        else:
            self.mqtt_state = "not_configured"
        await self._async_apply(resubscribe_mqtt=True)

    @callback
    def _on_connection(self, connected: bool) -> None:
        if self._stopped:
            return
        previous = self.mqtt_state
        self.mqtt_state = "connected" if connected else "disconnected"
        _LOGGER.debug("switch_mqtt_connection connected=%s", connected)
        if connected and previous != "connected":
            # A reconnect or an MQTT reload: subscribe again (retained replays are ignored).
            self._mqtt_ready = True
            self._create_task(self._async_apply(resubscribe_mqtt=True))
        self.ctrl.notify()

    # ------------------------------------------------------------ discovery
    @callback
    def async_request_bridge(self) -> None:
        """Read Zigbee2MQTT's device list (discovery). Kept while the integration runs."""
        if not self._bridge_wanted:
            self._bridge_wanted = True
            self._create_task(self._async_apply())

    @callback
    def async_config_changed(self) -> None:
        if self._started and not self._stopped:
            self._create_task(self._async_apply())

    @callback
    def async_registry_changed(self) -> None:
        """Devices or entities changed: device triggers may have been discovered."""
        if self._started and not self._stopped:
            self._create_task(self._async_apply(refresh_triggers=True))

    def topic_for(self, switch: SwitchConfig) -> str | None:
        """The Zigbee2MQTT device topic of a switch (manual topic, or base topic + friendly name)."""
        if switch.topic:
            return switch.topic
        z2m = self.bridge_devices.get(switch.ieee) if switch.ieee else None
        name = z2m.friendly_name if z2m else switch.z2m_name
        if not name:
            return None
        topic = f"{self.ctrl.lighting.switch_settings.mqtt_base_topic}/{name}"
        return topic if validate_topic(topic) is None else None

    def event_entity_for(self, switch: SwitchConfig) -> str | None:
        if switch.event_entity:
            return switch.event_entity
        found = event_entities(self.hass, switch.device_id)
        return next((e for e in found if "action" in e), found[0] if found else None)

    def triggers_for(self, switch: SwitchConfig) -> list[dict[str, Any]]:
        return self._triggers.get(switch.device_id or "", [])

    def removed_from_z2m(self, switch: SwitchConfig) -> bool:
        return bool(switch.ieee and self.bridge_seen_at and not switch.topic and switch.ieee not in self.bridge_devices)

    def _plan(self, area_id: str, switch: SwitchConfig) -> tuple[dict[str, Any], SwitchStatus]:
        """Which paths should listen for one switch now."""
        status = self.status.setdefault((area_id, switch.id), SwitchStatus())
        available: dict[str, Any] = {}
        topic = self.topic_for(switch)
        status.topic = topic
        status.problem = None
        if topic and self._mqtt_ready and not self.removed_from_z2m(switch):
            available[SwitchSource.ZIGBEE2MQTT.value] = topic
        if switch.device_id and self.triggers_for(switch) and self._mqtt_ready:
            available[SwitchSource.DEVICE_TRIGGER.value] = (
                switch.device_id, tuple(sorted((t["type"], t["subtype"]) for t in self.triggers_for(switch))))
        if (entity := self.event_entity_for(switch)) and (
                self.hass.states.get(entity) is not None or er.async_get(self.hass).async_get(entity) is not None):
            available[SwitchSource.EVENT_ENTITY.value] = entity
        status.available_sources = list(available)
        status.authoritative = resolve_source(switch, available)
        learning = (area_id, switch.id) in self.learning
        if learning:
            plan = available
        elif status.authoritative:
            plan = {status.authoritative: available[status.authoritative]}
        else:
            plan = {}
        if not plan:
            if self.removed_from_z2m(switch):
                status.problem = "The device is no longer in Zigbee2MQTT"
            elif switch.source != SwitchSource.AUTO and available:
                status.problem = f"The chosen input ({switch.source.value}) is not available"
            elif not self.mqtt_configured and (topic or switch.ieee):
                status.problem = "MQTT is not set up in Home Assistant"
            elif self.mqtt_configured and not self._mqtt_ready and (topic or switch.ieee):
                status.problem = "Waiting for MQTT"
            else:
                status.problem = "No input found: set an MQTT topic or an event entity (Advanced)"
        return plan, status

    async def _async_apply(self, *, resubscribe_mqtt: bool = False, refresh_triggers: bool = False) -> None:
        """Bring the subscriptions in line with the configuration (serialised; coalesced)."""
        if self._apply_lock.locked():
            self._apply_again = True
            if not (resubscribe_mqtt or refresh_triggers):
                return
        async with self._apply_lock:
            if self._stopped:
                return
            self._apply_again = False
            try:
                await self._async_apply_locked(resubscribe_mqtt, refresh_triggers)
            except Exception:
                _LOGGER.warning("Wall switch inputs could not be updated", exc_info=True)
        if self._apply_again and not self._stopped:
            self._apply_again = False
            await self._async_apply()

    async def _async_apply_locked(self, resubscribe_mqtt: bool, refresh_triggers: bool) -> None:
        switches = {(a, s.id): s for a, room in self.ctrl.lighting.rooms.items()
                    if a in self.ctrl.config.enabled_rooms for s in room.switches}
        devices = {s.device_id for s in switches.values() if s.device_id}
        if self._mqtt_ready and (refresh_triggers or set(self._triggers) - devices or not self._triggers_loaded(devices)):
            self._triggers = await async_device_triggers(self.hass, devices)
            self._triggers_known = set(devices)
        if self._stopped:
            return
        # The bridge device list: for discovery, renames and bindings.
        wants_bridge = self._mqtt_ready and (self._bridge_wanted or any(
            s.ieee and not s.topic for s in switches.values()))
        bridge_topic = f"{self.ctrl.lighting.switch_settings.mqtt_base_topic}/bridge/devices" if wants_bridge else None
        if bridge_topic != self._bridge_topic or (resubscribe_mqtt and bridge_topic):
            if self._bridge_unsub:
                self._safe(self._bridge_unsub)
                self._bridge_unsub = None
            self._bridge_topic = None
            if bridge_topic:
                self._bridge_unsub = await self._mqtt_subscribe(bridge_topic, self._on_bridge)
                self._bridge_topic = bridge_topic if self._bridge_unsub else None
        for key in [k for k in self._active if k not in switches]:
            self._unsubscribe_all(key)
            self._active.pop(key, None)
            self.status.pop(key, None)
            self.dedupe.forget(key[1])
            if session := self.learning.pop(key, None):
                session.cancel()
        event_entities_now: dict[str, list[Key]] = {}
        for key, switch in switches.items():
            plan, status = self._plan(key[0], switch)
            active = self._active.setdefault(key, _Active())
            for source in list(active.plan):
                if source not in plan or plan[source] != active.plan[source] or (
                        resubscribe_mqtt and source != SwitchSource.EVENT_ENTITY.value):
                    self._unsubscribe(key, source)
            for source, what in plan.items():
                if source == SwitchSource.EVENT_ENTITY.value:
                    event_entities_now.setdefault(what, []).append(key)
                    active.plan[source] = what
                    continue
                if source in active.plan:
                    continue
                if source == SwitchSource.ZIGBEE2MQTT.value:
                    unsubs = [u for u in (
                        await self._mqtt_subscribe(what, self._mqtt_handler(key)),
                        await self._mqtt_subscribe(f"{what}/availability", self._availability_handler(key)),
                    ) if u]
                else:
                    unsubs = [u for u in [await self._attach_triggers(key, switch)] if u]
                if self._stopped:
                    for u in unsubs:
                        self._safe(u)
                    return
                if unsubs:
                    active.unsubs[source] = unsubs
                    active.plan[source] = what
                    active.settle_until[source] = time.monotonic() + ATTACH_SETTLE_SECONDS
            status.sources = list(active.plan)
            if SwitchSource.EVENT_ENTITY.value in plan:
                entity = plan[SwitchSource.EVENT_ENTITY.value]
                state = self.hass.states.get(entity)
                if SwitchSource.ZIGBEE2MQTT.value not in plan:
                    status.availability = "unavailable" if state is None or state.state == STATE_UNAVAILABLE else None
        if set(event_entities_now) != set(self._event_entities):
            if self._event_unsub:
                self._event_unsub()
                self._event_unsub = None
            if event_entities_now:
                self._event_unsub = async_track_state_change_event(
                    self.hass, sorted(event_entities_now), self._on_event_entity)
        self._event_entities = event_entities_now
        self.ctrl.notify()

    def _triggers_loaded(self, devices: set[str]) -> bool:
        return devices <= self._triggers_known

    @staticmethod
    def _safe(unsub: CALLBACK_TYPE) -> None:
        try:
            unsub()
        except Exception:
            _LOGGER.debug("switch_unsubscribe_failed", exc_info=True)

    def _unsubscribe(self, key: Key, source: str) -> None:
        active = self._active.get(key)
        if active is None:
            return
        for unsub in active.unsubs.pop(source, []):
            self._safe(unsub)
        active.plan.pop(source, None)
        active.settle_until.pop(source, None)

    def _unsubscribe_all(self, key: Key) -> None:
        for source in list((self._active.get(key) or _Active()).plan):
            self._unsubscribe(key, source)

    async def _mqtt_subscribe(self, topic: str, handler) -> CALLBACK_TYPE | None:
        from homeassistant.components import mqtt

        if validate_topic(topic) is not None and not topic.endswith("/bridge/devices"):
            return None
        try:
            return await mqtt.async_subscribe(self.hass, topic, handler, qos=0, encoding=None)
        except Exception as err:  # noqa: BLE001 - MQTT not (yet) available
            _LOGGER.debug("switch_subscribe_failed topic=%s error=%s", topic, err)
            return None

    async def _attach_triggers(self, key: Key, switch: SwitchConfig) -> CALLBACK_TYPE | None:
        from homeassistant.helpers.trigger import (
            async_initialize_triggers,
            async_validate_trigger_config,
        )

        configs = [
            {"platform": "device", "domain": "mqtt", "device_id": switch.device_id, "type": t["type"],
             "subtype": t["subtype"], "id": t["subtype"]}
            for t in self.triggers_for(switch)
        ]
        try:
            validated = await async_validate_trigger_config(self.hass, configs)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("switch_trigger_invalid switch=%s error=%s", switch.id, err)
            return None

        async def fire(run_variables: dict[str, Any], context=None) -> None:
            trigger = run_variables.get("trigger") or {}
            raw = trigger.get("id")
            if isinstance(raw, str):
                self._emit(key, SwitchSource.DEVICE_TRIGGER.value, raw)

        def log(level: int, msg: str, **kwargs: Any) -> None:
            _LOGGER.log(min(level, logging.DEBUG), "switch_triggers %s: %s", switch.id, msg)

        try:
            return await async_initialize_triggers(self.hass, validated, fire, "smart_occupancy",
                                                   f"wall switch {switch.name}", log)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("switch_trigger_attach_failed switch=%s error=%s", switch.id, err)
            return None

    # --------------------------------------------------------------- inputs
    def _mqtt_handler(self, key: Key):
        @callback
        def handle(msg) -> None:
            try:
                if msg.retain:
                    self._ignore(key, SwitchSource.ZIGBEE2MQTT.value, "retained")
                    return
                action, reason = parse_mqtt_payload(msg.payload)
                if action is None:
                    self._ignore(key, SwitchSource.ZIGBEE2MQTT.value, reason)
                    return
                self._emit(key, SwitchSource.ZIGBEE2MQTT.value, action)
            except Exception:
                _LOGGER.debug("switch_message_failed switch=%s", key[1], exc_info=True)

        return handle

    def _availability_handler(self, key: Key):
        @callback
        def handle(msg) -> None:
            payload = msg.payload.decode("utf-8", errors="replace") if isinstance(msg.payload, bytes) else str(msg.payload)
            value = payload.strip()
            if value.startswith("{"):
                try:
                    value = str((json.loads(value) or {}).get("state") or "")
                except (ValueError, AttributeError):
                    value = ""
            status = self.status.get(key)
            if value in ("online", "offline") and status is not None and status.availability != value:
                status.availability = value
                self.ctrl.notify()

        return handle

    @callback
    def _on_bridge(self, msg) -> None:
        devices, coordinator = parse_bridge_devices(msg.payload)
        if not devices and not coordinator:
            return
        self.bridge_devices, self.coordinator = devices, coordinator
        self.bridge_seen_at = _iso()
        # Follow Zigbee2MQTT renames (by IEEE address): the topic changes with the name.
        lighting = self.ctrl.lighting
        updated = lighting
        for area_id, room in lighting.rooms.items():
            for switch in room.switches:
                z2m = devices.get(switch.ieee) if switch.ieee else None
                if z2m and z2m.friendly_name != switch.z2m_name:
                    _LOGGER.info("switch_renamed switch=%s old=%s new=%s", switch.id, switch.z2m_name,
                                 z2m.friendly_name)
                    updated = updated.with_room(area_id, updated.room(area_id).with_switch(
                        replace(switch, z2m_name=z2m.friendly_name)))
        if updated != lighting:
            self.ctrl.store.async_set(updated)  # resubscribes through the configuration listener
        else:
            self._create_task(self._async_apply())

    @callback
    def _on_event_entity(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        for key in self._event_entities.get(entity_id, ()):
            status = self.status.get(key)
            if status is not None and SwitchSource.ZIGBEE2MQTT.value not in status.sources:
                unavailable = new is None or new.state == STATE_UNAVAILABLE
                if (status.availability == "unavailable") != unavailable:
                    status.availability = "unavailable" if unavailable else None
                    self.ctrl.notify()
            if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            if old is None or old.state == STATE_UNAVAILABLE or old.state == new.state:
                # Restored or reconnected (not a new press), or an attribute-only update.
                if old is not None and old.state != new.state:
                    self._ignore(key, SwitchSource.EVENT_ENTITY.value, "stale")
                continue
            fired = dt_util.parse_datetime(new.state)
            if fired is not None and (dt_util.utcnow() - fired).total_seconds() > STALE_EVENT_SECONDS:
                self._ignore(key, SwitchSource.EVENT_ENTITY.value, "stale")
                continue
            raw = event_entity_action(dict(new.attributes))
            if raw is None:
                self._ignore(key, SwitchSource.EVENT_ENTITY.value, "invalid_action")
                continue
            self._emit(key, SwitchSource.EVENT_ENTITY.value, raw, ref=f"{entity_id}@{new.state}")

    def _ignore(self, key: Key, source: str, reason: str) -> None:
        status = self.status.get(key)
        if status is None:
            return
        if reason == "no_action":
            status.ignored_updates += 1  # battery / link quality: counted, not shown
            return
        status.last_ignored = {"at": _iso(), "source": source, "reason": reason,
                               "text": IGNORED_WORDS.get(reason, reason)}
        _LOGGER.debug("switch_event_ignored switch=%s source=%s reason=%s", key[1], source, reason)
        self.ctrl.notify()

    def _emit(self, key: Key, source: str, raw: str, *, ref: str | None = None) -> None:
        """A press arrived: settle, deduplicate, capture (learning) or hand to the controller."""
        active = self._active.get(key)
        now = time.monotonic()
        if active is not None and now < active.settle_until.get(source, 0) and source == SwitchSource.DEVICE_TRIGGER.value:
            self._ignore(key, source, "settling")
            return
        area_id, switch_id = key
        switch = self.ctrl.lighting.room(area_id).switch(switch_id)
        if switch is None:
            return
        event = ButtonEvent.create(area_id, switch_id, source, raw, now, _iso(), ref)
        accepted, reason = self.dedupe.accept(event, switch.debounce_ms)
        if not accepted:
            self._ignore(key, source, reason or "duplicate")
            return
        status = self.status.setdefault(key, SwitchStatus())
        status.last_event = event.as_dict()
        if (session := self.learning.get(key)) is not None:
            self._capture(key, switch, event, session)
            return
        try:
            self._handler(event)
        except Exception:
            _LOGGER.warning("Wall switch %s: handling %s failed", switch.name, raw, exc_info=True)

    # ------------------------------------------------------------- learning
    @callback
    def async_start_learning(self, area_id: str, switch_id: str, seconds: int = DEFAULT_LEARN_SECONDS) -> LearnSession:
        key = (area_id, switch_id)
        self.async_stop_learning(area_id, switch_id, apply=False)
        seconds = min(max(int(seconds), 5), MAX_LEARN_SECONDS)

        @callback
        def expire(_now: datetime) -> None:
            if self.learning.get(key) is session:
                _LOGGER.debug("switch_learning_timeout switch=%s", switch_id)
                self.async_stop_learning(area_id, switch_id)

        session = LearnSession(until=dt_util.utcnow() + timedelta(seconds=seconds), cancel=lambda: None)
        session.cancel = async_call_later(self.hass, seconds, expire)
        self.learning[key] = session
        _LOGGER.info("switch_learning_started switch=%s seconds=%d", switch_id, seconds)
        self._create_task(self._async_apply())
        self.ctrl.notify()
        return session

    @callback
    def async_stop_learning(self, area_id: str, switch_id: str, *, apply: bool = True) -> list[dict[str, Any]]:
        session = self.learning.pop((area_id, switch_id), None)
        if session is None:
            return []
        session.cancel()
        _LOGGER.info("switch_learning_stopped switch=%s captured=%d", switch_id, len(session.captured))
        if apply:
            self._create_task(self._async_apply())
            self.ctrl.notify()
        return session.captured

    def _capture(self, key: Key, switch: SwitchConfig, event: ButtonEvent, session: LearnSession) -> None:
        """Learning: remember the button (persisted at once); never execute anything."""
        status = self.status[key]
        new = event.raw not in switch.buttons
        session.captured.append({**event.as_dict(), "new": new})
        del session.captured[:-20]
        status.last_result, status.last_result_ok = "Captured by Learn buttons (no command)", None
        status.history.append({"at": event.when, "raw": event.raw, "label": action_label(event.raw),
                               "result": "learned", "source": event.source})
        if new:
            area_id = key[0]
            lighting = self.ctrl.lighting
            room = lighting.room(area_id)
            self.ctrl.store.async_set(lighting.with_room(area_id, room.with_switch(
                switch.with_button(event.raw, event.source, event.when))))
        self.ctrl.notify()
