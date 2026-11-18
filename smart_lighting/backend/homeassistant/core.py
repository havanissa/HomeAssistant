"""Core objects of the host interface: states, contexts, events, the event bus, services and ``HomeAssistant``.

The semantics follow Home Assistant's own where the vendored lighting runtime
depends on them:

* listeners decorated with :func:`callback` run synchronously, in registration
  order, when an event is dispatched; coroutine functions are scheduled as tasks;
* state objects are immutable snapshots (``last_changed`` and ``last_updated``
  are Home Assistant's, parsed from the event; ``context`` is the context the
  change was made with);
* ``Context()`` creates a new ULID, exactly like Home Assistant, so commands sent
  with it are attributed to it.

Nothing here talks to Home Assistant directly. Remote operations (service calls,
trigger subscriptions, MQTT, device automations, options) go through the
:class:`HostBackend` the App installs (``smart_lighting.lighting_host``).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine, Iterable
from datetime import datetime
import enum
import functools
import inspect
import logging
import os
import time
from typing import Any, Generic, TypeVar, TypedDict

from .const import EVENT_STATE_CHANGED, MATCH_ALL
from .exceptions import HomeAssistantError
from .util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CALLBACK_TYPE = Callable[[], None]
_T = TypeVar("_T")
_DataT = TypeVar("_DataT")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid_now() -> str:
    """A ULID (48-bit milliseconds + 80 random bits, Crockford base32), as Home Assistant creates them."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def callback(func: _T) -> _T:
    """Mark a function as safe to run in the event loop (as in Home Assistant)."""
    setattr(func, "_hass_callback", True)
    return func


def is_callback(func: Any) -> bool:
    return getattr(func, "_hass_callback", False) is True


def split_entity_id(entity_id: str) -> tuple[str, str]:
    domain, _, object_id = entity_id.partition(".")
    if not domain or not object_id:
        raise ValueError(f"Invalid entity ID {entity_id}")
    return domain, object_id


class CoreState(enum.Enum):
    not_running = "NOT_RUNNING"
    starting = "STARTING"
    running = "RUNNING"
    stopping = "STOPPING"
    final_write = "FINAL_WRITE"
    stopped = "STOPPED"

    def __str__(self) -> str:
        return self.value


class Context:
    """The context of a change: who (user), what started it (parent) and its unique id."""

    __slots__ = ("id", "user_id", "parent_id", "origin_event")

    def __init__(self, user_id: str | None = None, parent_id: str | None = None, id: str | None = None) -> None:  # noqa: A002
        self.id = id or ulid_now()
        self.user_id = user_id
        self.parent_id = parent_id
        self.origin_event: Event | None = None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Context) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"<Context id={self.id} user={self.user_id} parent={self.parent_id}>"

    def as_dict(self) -> dict[str, str | None]:
        return {"id": self.id, "parent_id": self.parent_id, "user_id": self.user_id}

    @classmethod
    def from_wire(cls, data: Any) -> Context:
        if not isinstance(data, dict):
            return cls()
        return cls(user_id=data.get("user_id"), parent_id=data.get("parent_id"), id=data.get("id") or None)


class State:
    """One entity state (immutable)."""

    __slots__ = ("entity_id", "state", "attributes", "last_changed", "last_updated", "last_reported", "context",
                 "domain", "object_id")

    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any] | None = None,
                 last_changed: datetime | None = None, last_updated: datetime | None = None,
                 context: Context | None = None, last_reported: datetime | None = None) -> None:
        now = dt_util.utcnow()
        self.entity_id = entity_id.lower()
        self.domain, self.object_id = split_entity_id(self.entity_id)
        self.state = state
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.last_updated = last_updated or now
        self.last_changed = last_changed or self.last_updated
        self.last_reported = last_reported or self.last_updated
        self.context = context or Context()

    @property
    def name(self) -> str:
        return self.attributes.get("friendly_name") or self.object_id.replace("_", " ")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": dict(self.attributes),
            "last_changed": self.last_changed.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "context": self.context.as_dict(),
        }

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, State) and self.entity_id == other.entity_id and self.state == other.state
                and self.attributes == other.attributes)

    def __repr__(self) -> str:
        return f"<state {self.entity_id}={self.state} @ {self.last_changed.isoformat()}>"

    @classmethod
    def from_wire(cls, data: Any) -> State | None:
        """A state from Home Assistant's websocket representation (``get_states`` / ``state_changed``)."""
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str):
            return None
        try:
            return cls(
                data["entity_id"],
                str(data.get("state")),
                data.get("attributes") if isinstance(data.get("attributes"), dict) else {},
                last_changed=_time(data.get("last_changed")),
                last_updated=_time(data.get("last_updated")),
                last_reported=_time(data.get("last_reported")),
                context=Context.from_wire(data.get("context")),
            )
        except ValueError:
            return None


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


class EventStateChangedData(TypedDict):
    entity_id: str
    old_state: State | None
    new_state: State | None


class Event(Generic[_DataT]):
    """An event on the bus."""

    __slots__ = ("event_type", "data", "origin", "time_fired", "context")

    def __init__(self, event_type: str, data: Any = None, origin: str = "REMOTE", time_fired: datetime | None = None,
                 context: Context | None = None) -> None:
        self.event_type = event_type
        self.data = data or {}
        self.origin = origin
        self.time_fired = time_fired or dt_util.utcnow()
        self.context = context or Context()

    def __repr__(self) -> str:
        return f"<Event {self.event_type}>"


def _run(hass: HomeAssistant, func: Callable[..., Any], *args: Any) -> None:
    """Call a listener the way Home Assistant does: callbacks now, coroutine functions as tasks."""
    target = func.func if isinstance(func, functools.partial) else func
    try:
        if inspect.iscoroutinefunction(target):
            hass.async_create_task(func(*args))
            return
        result = func(*args)
        if asyncio.iscoroutine(result):
            hass.async_create_task(result)
    except Exception:  # noqa: BLE001 - one listener never breaks the others (as in Home Assistant)
        _LOGGER.exception("Error in listener %s", getattr(func, "__qualname__", func))


class EventBus:
    """Synchronous dispatch to listeners of an event type (``*`` listens to every event)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._listeners: dict[str, list[tuple[Callable[[Event], Any], Callable[[Any], bool] | None]]] = {}
        self._dispatching = False
        self._queue: deque[Event] = deque()

    def async_listen(self, event_type: str, listener: Callable[[Event], Any],
                     event_filter: Callable[[Any], bool] | None = None, run_immediately: Any = None) -> CALLBACK_TYPE:
        item = (listener, event_filter)
        self._listeners.setdefault(event_type, []).append(item)

        def remove() -> None:
            items = self._listeners.get(event_type, [])
            if item in items:
                items.remove(item)

        return remove

    def async_listen_once(self, event_type: str, listener: Callable[[Event], Any]) -> CALLBACK_TYPE:
        remove: CALLBACK_TYPE | None = None
        fired = False

        def once(event: Event) -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            if remove is not None:
                remove()
            _run(self._hass, listener, event)

        remove = self.async_listen(event_type, once)
        return remove

    def async_listeners(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._listeners.items() if v}

    def async_fire_internal(self, event: Event) -> None:
        """Dispatch an event received from Home Assistant (or created by the host).

        As in Home Assistant, an event fired while another is being dispatched is queued and dispatched after it
        (in order), not inside the listener that fired it.
        """
        if self._dispatching:
            self._queue.append(event)
            return
        self._dispatching = True
        try:
            self._dispatch(event)
            while self._queue:
                self._dispatch(self._queue.popleft())
        finally:
            self._dispatching = False
            self._hass.event_time = None

    def _dispatch(self, event: Event) -> None:
        # Timers started by the listeners count from when the event fired in Home Assistant, not from when it
        # reached the App (``helpers.event.async_call_later``): a debounce then ends at the same instant as in the
        # integration, before the integration's reaction (sent after its debounce) can reach the App.
        self._hass.event_time = event.time_fired
        for key in (event.event_type, MATCH_ALL):
            for listener, event_filter in list(self._listeners.get(key, ())):
                if event_filter is not None:
                    try:
                        if not event_filter(event.data):
                            continue
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Error in event filter")
                        continue
                _run(self._hass, listener, event)


class StateMachine:
    """The host's copy of Home Assistant's states (fed by ``get_states`` and ``state_changed``)."""

    def __init__(self) -> None:
        self._states: dict[str, State] = {}

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id.lower()) if isinstance(entity_id, str) else None

    def is_state(self, entity_id: str, state: str) -> bool:
        current = self.get(entity_id)
        return current is not None and current.state == state

    def async_all(self, domain_filter: str | Iterable[str] | None = None) -> list[State]:
        if domain_filter is None:
            return list(self._states.values())
        domains = {domain_filter} if isinstance(domain_filter, str) else {d.lower() for d in domain_filter}
        return [s for s in self._states.values() if s.domain in domains]

    def async_entity_ids(self, domain_filter: str | Iterable[str] | None = None) -> list[str]:
        return [s.entity_id for s in self.async_all(domain_filter)]

    def async_entity_ids_count(self, domain_filter: str | Iterable[str] | None = None) -> int:
        return len(self.async_all(domain_filter))

    # The host's interface -------------------------------------------------------
    def replace_all(self, states: Iterable[State]) -> None:
        self._states = {s.entity_id: s for s in states}

    def set_state(self, entity_id: str, state: State | None) -> State | None:
        """Store a new state; returns the previous one."""
        entity_id = entity_id.lower()
        old = self._states.get(entity_id)
        if state is None:
            self._states.pop(entity_id, None)
        else:
            self._states[entity_id] = state
        return old


class ServiceRegistry:
    """Service calls, forwarded to the host backend (the only way out of the runtime)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_call(self, domain: str, service: str, service_data: dict[str, Any] | None = None,
                         blocking: bool = False, context: Context | None = None, target: dict[str, Any] | None = None,
                         return_response: bool = False) -> Any:
        backend = self._hass.backend
        if backend is None:
            raise HomeAssistantError("No connection to Home Assistant")
        data = dict(service_data or {})
        if target:
            data.update(target)
        return await backend.async_call_service(domain, service, data, context or Context(), blocking)

    def has_service(self, domain: str, service: str) -> bool:
        return True


class UnitSystem:
    """The unit system's units the runtime reads (``hass.config.units``), from ``get_config``'s ``unit_system``."""

    def __init__(self, temperature_unit: str = "°C", length_unit: str = "km") -> None:
        self.temperature_unit = temperature_unit
        self.length_unit = length_unit

    @classmethod
    def from_wire(cls, data: Any) -> UnitSystem:
        data = data if isinstance(data, dict) else {}
        return cls(str(data.get("temperature") or "°C"), str(data.get("length") or "km"))


class Config:
    """The parts of Home Assistant's configuration the runtime reads (from ``get_config``)."""

    def __init__(self) -> None:
        self.components: set[str] = set()
        self.latitude: float = 0.0
        self.longitude: float = 0.0
        self.elevation: int = 0
        self.time_zone: str = "UTC"
        self.location_name: str = "Home"
        self.language: str = "en"
        self.units: UnitSystem = UnitSystem()

    def update_from_wire(self, data: dict[str, Any]) -> None:
        self.components = {str(c) for c in data.get("components") or ()}
        if "unit_system" in data:
            self.units = UnitSystem.from_wire(data.get("unit_system"))
        for key in ("latitude", "longitude"):
            if isinstance(data.get(key), (int, float)):
                setattr(self, key, float(data[key]))
        if isinstance(data.get("elevation"), (int, float)):
            self.elevation = int(data["elevation"])
        if isinstance(data.get("time_zone"), str):
            self.time_zone = data["time_zone"]
            dt_util.set_default_time_zone(dt_util.get_time_zone(self.time_zone) or dt_util.UTC)
        if isinstance(data.get("location_name"), str):
            self.location_name = data["location_name"]
        if isinstance(data.get("language"), str):
            self.language = data["language"]


class HostBackend:
    """What the host must provide for remote operations (implemented by the App; tests use a simulator)."""

    async def async_call_service(self, domain: str, service: str, data: dict[str, Any], context: Context,
                                 blocking: bool) -> Any:
        raise HomeAssistantError("service calls are not available")

    async def async_validate_triggers(self, configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise HomeAssistantError("triggers are not available")

    async def async_subscribe_triggers(self, configs: list[dict[str, Any]],
                                       action: Callable[[dict[str, Any], Context | None], Any]) -> CALLBACK_TYPE:
        raise HomeAssistantError("triggers are not available")

    async def async_device_triggers(self, device_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {}

    async def async_mqtt_subscribe(self, topic: str, handler: Callable[[Any], None]) -> CALLBACK_TYPE:
        raise HomeAssistantError("MQTT is not available")

    def mqtt_connected(self) -> bool:
        return False

    def async_update_options(self, entry: Any, options: dict[str, Any]) -> bool:
        raise HomeAssistantError("the integration's options cannot be changed from here")


class HomeAssistant:
    """The runtime's view of Home Assistant."""

    def __init__(self, backend: HostBackend | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        from .config_entries import ConfigEntries  # noqa: PLC0415 - avoid an import cycle

        self.loop = loop or asyncio.get_running_loop()
        self.backend = backend
        self.states = StateMachine()
        self.bus = EventBus(self)
        # When the event being dispatched fired in Home Assistant (see EventBus._dispatch).
        self.event_time: datetime | None = None
        self.services = ServiceRegistry(self)
        self.config = Config()
        self.config_entries = ConfigEntries(self)
        self.data: dict[str, Any] = {}
        self.state = CoreState.not_running
        self._tasks: set[asyncio.Task[Any]] = set()
        self.storage_dir: str | None = None

    @property
    def is_running(self) -> bool:
        return self.state in (CoreState.starting, CoreState.running)

    @property
    def is_stopping(self) -> bool:
        return self.state in (CoreState.stopping, CoreState.final_write, CoreState.stopped)

    def async_create_task(self, coro: Coroutine[Any, Any, _T], name: str | None = None,
                          eager_start: bool = True) -> asyncio.Task[_T]:
        """As in Home Assistant, a task starts eagerly: it runs now until its first suspension."""
        task = asyncio.Task(coro, loop=self.loop, name=name, eager_start=eager_start)
        if not task.done():
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return task

    def async_create_background_task(self, coro: Coroutine[Any, Any, _T], name: str,
                                     eager_start: bool = True) -> asyncio.Task[_T]:
        return self.async_create_task(coro, name)

    async def async_add_executor_job(self, target: Callable[..., _T], *args: Any) -> _T:
        return await self.loop.run_in_executor(None, target, *args)

    def async_run_hass_job(self, job: Any, *args: Any) -> None:
        _run(self, getattr(job, "target", job), *args)

    # The host's interface -------------------------------------------------------
    def async_state_changed(self, entity_id: str, new_state: State | None, *,
                            time_fired: datetime | None = None) -> None:
        """Apply a state change from Home Assistant and dispatch ``state_changed`` (with its context)."""
        old = self.states.set_state(entity_id, new_state)
        if old is None and new_state is None:
            return
        context = new_state.context if new_state is not None else Context()
        self.bus.async_fire_internal(Event(
            EVENT_STATE_CHANGED, {"entity_id": entity_id.lower(), "old_state": old, "new_state": new_state},
            time_fired=time_fired or (new_state.last_updated if new_state else None), context=context))

    def async_state_reported(self, entity_id: str, reported: State) -> None:
        """An unchanged report (Home Assistant's ``state_reported``: same state and attributes, only
        ``last_reported`` moved). As in Home Assistant no ``state_changed`` is fired; the stored state is replaced
        by one with the new ``last_reported`` (state objects stay immutable)."""
        current = self.states.get(entity_id)
        if current is None or current.state != reported.state or current.attributes != reported.attributes:
            return
        if reported.last_reported <= current.last_reported:
            return
        self.states.set_state(entity_id, State(
            current.entity_id, current.state, current.attributes, last_changed=current.last_changed,
            last_updated=current.last_updated, context=current.context, last_reported=reported.last_reported))

    async def async_cancel_tasks(self) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
