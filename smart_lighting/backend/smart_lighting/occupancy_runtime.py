"""The App's occupancy runtime (Phase A3.2): the shared engine, event driven, in shadow or as the owner.

The engine is the shared package ``occupancy_core`` (a byte-identical copy of
the integration's). This runtime only connects it:

* **Events** - Home Assistant's ``state_changed`` events, which the App
  already receives, at Home Assistant's ``last_changed``; one asyncio timer per
  room at its next deadline; no polling.
* **Configuration** - the integration's (the only source): the bridge session's
  ``hello`` and ``spec`` events carry the engines' configuration and its
  revision. The App keeps no configuration of its own.
* **Inputs that are not sensors** - preliminary lighting signals, zone holds
  and wall-switch activity are produced by the integration's lighting runtime
  and streamed over the bridge, with their time.
* **Reference start** - every bridge session begins with an exact copy of the
  integration engine's state (``export_state``), so after a reconnect, an
  integration reload or a Home Assistant restart nothing is replayed and
  nothing is invented; then both engines run on the same events and the
  comparison (``occupancy_compare``) measures them.
* **Ownership** - only the integration decides who publishes occupancy. The App
  publishes its results only after a reviewed handover (``claim``), with the
  lease token, epoch, increasing sequence numbers and the configuration
  revision; a refusal that means "not the owner" returns it to shadow mode at
  once. A new App process is always a new instance: it never resumes an
  ownership; an App that lost ownership never takes it back by itself.
* **Storage** - ``/data/occupancy/state.json`` (versioned: the decisions and
  the configuration they belong to, written on transitions only, coalesced; the
  previous file is kept as ``state.json.bak``) is used only while the
  integration's engine cannot be reached; ``comparison.json`` keeps the bounded
  discrepancy history.

It never sends a service call: the Home Assistant client offers no command
that could (``ha.py``).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any

from occupancy_core import CORE_VERSION, fingerprint, golden
from occupancy_core import protocol as P
from occupancy_core.engine import StateError
from occupancy_core.house import House, HouseSpec, Observation
from occupancy_core.wire import WireError, iso, parse_time, result_to_wire, spec_from_wire, spec_to_wire

from .ha import HomeAssistantClient, HomeAssistantError
from .occupancy_compare import Comparator, brief

_LOGGER = logging.getLogger(__name__)

STATE_SCHEMA = 1
MIN_SHADOW_SECONDS = 2 * 3600
MIN_TRANSITIONS = 20
SAVE_DELAY = 15.0
COMPARISON_SAVE_DELAY = 60.0
RESUBSCRIBE_DELAYS = (2, 5, 15, 30, 60)
LOST_CODES = frozenset({P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH, P.REJECT_LEASE, P.REJECT_UNKNOWN_SESSION})
OWNERSHIP_LOG = 50


class EngineError(Exception):
    """A handover or rollback request was refused (``code`` is stable)."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, data: dict[str, Any], *, backup: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if backup and path.exists():
        os.replace(path, path.with_suffix(path.suffix + ".bak"))
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not read %s: %s", path, err)
        return {"_unreadable": str(err)}


class OccupancyRuntime:
    """One shared-engine House, its timers, the bridge session and the comparison."""

    def __init__(self, ha: HomeAssistantClient, data_dir: Path | None, *, app_version: str,
                 min_shadow_seconds: int = MIN_SHADOW_SECONDS, min_transitions: int = MIN_TRANSITIONS) -> None:
        self.ha = ha
        self.dir = data_dir / "occupancy" if data_dir else None
        self.app_version = app_version
        self.min_shadow_seconds = min_shadow_seconds
        self.min_transitions = min_transitions
        self.instance_id = secrets.token_hex(16)  # new for every App process: it never resumes an old ownership
        self.fingerprint: str | None = None
        self.self_test: dict[str, Any] | None = None
        self.house: House | None = None
        self.mode = "starting"  # starting | standalone | shadow | authoritative
        self.bridge = "disconnected"  # disconnected | subscribing | connected | unsupported | incompatible | error
        self.bridge_error: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.integration: dict[str, Any] = {}  # the integration's ownership record, as last reported
        self.integration_results: dict[str, dict[str, Any]] = {}
        self.result_fired: dict[str, Any] = {}  # when the integration's result fired (ISO), per room
        self.results: dict[str, dict[str, Any]] = {}
        self.epoch: int | None = None
        self._token: str | None = None
        self.seq = 0
        self.lease_until: str | None = None
        self.comparator = Comparator()
        self.counters: Counter[str] = Counter()
        self.last_publish_at: datetime | None = None
        self.last_publish_error: dict[str, Any] | None = None
        self.notices: list[dict[str, str]] = []
        self.ownership_log: deque[dict[str, Any]] = deque(maxlen=OWNERSHIP_LOG)
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._pending: set[str] = set()
        self._publish_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeat: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None
        self._save_handle: asyncio.TimerHandle | None = None
        self._compare_handle: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[str], None]] = []
        # Phase A4: the lighting runtime follows the published occupancy of each room.
        self._result_listeners: list[Callable[[str], None]] = []
        self._unsub: Callable[[], None] | None = None
        self._attempt = 0
        self._stopping = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.fingerprint = await asyncio.to_thread(fingerprint)
        self.self_test = await asyncio.to_thread(golden.self_test)
        if not self.self_test["passed"]:
            _LOGGER.error("Occupancy engine self-test failed: %s", self.self_test["failed"])
            self._notice("self_test_failed", "The engine self-test failed: a handover is not possible")
        await self._load()
        self._unsub = self.ha.add_listener(self._on_ha)
        if self.ha.connected:
            self._connected()
        _LOGGER.info("Occupancy runtime started (engine %s, instance %s, mode %s); self-test: %d scenarios %s",
                     CORE_VERSION, self.instance_id[:8], self.mode, self.self_test["scenarios"],
                     "passed" if self.self_test["passed"] else "FAILED")

    async def stop(self) -> None:
        self._stopping = True
        if self._unsub:
            self._unsub()
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for task in [self._heartbeat, self._subscribe_task, *self._tasks]:
            if task is not None:
                task.cancel()
        for task in [self._heartbeat, self._subscribe_task, *self._tasks]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        for handle in (self._save_handle, self._compare_handle):
            if handle:
                handle.cancel()
        await self._save_state()
        await self._save_comparison()

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """``listener(kind)``: "engine" (mode, ownership or connection changed)."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # ------------------------------------------------- published occupancy (A4)
    def published_wire(self, area_id: str) -> dict[str, Any] | None:
        """The published result of a room: the App's while it owns occupancy, otherwise the integration's."""
        if self.mode == "authoritative":
            return self.results.get(area_id)
        return self.integration_results.get(area_id)

    def room_spec(self, area_id: str) -> Any:
        return self.house.spec.room(area_id) if self.house is not None else None

    def add_result_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._result_listeners.append(listener)
        return lambda: self._result_listeners.remove(listener) if listener in self._result_listeners else None

    def _published_changed(self, area_ids: Any) -> None:
        for area_id in list(area_ids):
            for listener in list(self._result_listeners):
                try:
                    listener(area_id)
                except Exception:  # noqa: BLE001 - one listener must not break occupancy
                    _LOGGER.exception("Occupancy result listener failed")

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener("engine")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Engine listener failed")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _notice(self, code: str, text: str) -> None:
        if not any(n["code"] == code for n in self.notices):
            self.notices.append({"code": code, "text": text, "at": iso(utcnow()) or ""})

    def _log_ownership(self, event: str, **data: Any) -> None:
        self.ownership_log.appendleft({"at": iso(utcnow()), "event": event, **data})

    # --------------------------------------------------------------- storage
    async def _load(self) -> None:
        if self.dir is None:
            self.mode = "standalone"
            return
        data = await asyncio.to_thread(_read, self.dir / "state.json")
        compared = await asyncio.to_thread(_read, self.dir / "comparison.json")
        self.comparator.restore(compared)
        self.mode = "standalone"
        if data is None:
            return
        try:
            if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
                raise ValueError(f"schema {data.get('schema') if isinstance(data, dict) else None!r}")
            if data.get("core_version") != CORE_VERSION:
                raise ValueError(f"stored by engine {data.get('core_version')!r}")
            spec = spec_from_wire(data.get("spec"))
            rooms = data.get("rooms")
            if not isinstance(rooms, dict):
                raise ValueError("rooms missing")
        except (ValueError, WireError) as err:
            # Never invent a state: start without one (every room uncertain until its sensors report).
            _LOGGER.warning("The stored occupancy state is not usable (%s); starting without it", err)
            self._notice("state_incompatible", f"The stored occupancy state was not used ({err})")
            return
        house = House(spec)
        # Restart semantics of the engine: an occupied room stays occupied (start-up hold, new quiet period).
        house.load({a: r for a, r in rooms.items() if isinstance(r, dict)}, utcnow(), self._lookup, self._name_of)
        self.house = house

    def _state_data(self) -> dict[str, Any] | None:
        if self.house is None:
            return None
        return {"schema": STATE_SCHEMA, "core_version": CORE_VERSION, "fingerprint": self.fingerprint,
                "saved_at": iso(utcnow()), "spec": spec_to_wire(self.house.spec), "rooms": self.house.persist()}

    def _save_soon(self) -> None:
        if self.dir is None or self._save_handle is not None:
            return
        self._save_handle = asyncio.get_running_loop().call_later(
            SAVE_DELAY, lambda: self._spawn(self._save_state()))

    async def _save_state(self) -> None:
        self._save_handle = None
        data = self._state_data()
        if self.dir is None or data is None:
            return
        try:
            await asyncio.to_thread(_atomic_write, self.dir / "state.json", data, backup=True)
            self.counters["state_saves"] += 1
        except OSError as err:
            _LOGGER.warning("Could not store the occupancy state: %s", err)

    def _compare_soon(self) -> None:
        if self.dir is None or self._compare_handle is not None or not self.comparator.dirty:
            return
        self._compare_handle = asyncio.get_running_loop().call_later(
            COMPARISON_SAVE_DELAY, lambda: self._spawn(self._save_comparison()))

    async def _save_comparison(self) -> None:
        self._compare_handle = None
        if self.dir is None:
            return
        try:
            await asyncio.to_thread(_atomic_write, self.dir / "comparison.json", self.comparator.persist(),
                                    backup=False)
        except OSError as err:
            _LOGGER.warning("Could not store the comparison: %s", err)

    # ---------------------------------------------------------- HA events
    def _lookup(self, entity_id: str) -> Observation | None:
        state = self.ha.states.get(entity_id)
        if not state:
            return None
        return Observation(state.get("state"), _time(state.get("last_changed")),
                           (state.get("attributes") or {}).get("friendly_name"))

    def _name_of(self, entity_id: str) -> str | None:
        obs = self._lookup(entity_id)
        if obs and obs.name:
            return obs.name
        reg = next((e for e in self.ha.entity_registry if e.get("entity_id") == entity_id), None)
        return (reg or {}).get("name") or (reg or {}).get("original_name")

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            self._on_state(*payload)
        elif kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._disconnected("Home Assistant connection lost")

    def _on_state(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        house = self.house
        if house is None or not house.rooms_of(entity_id):
            return
        now = utcnow()
        obs = None
        if new_state is not None:
            obs = Observation(new_state.get("state"), _time(new_state.get("last_changed")),
                              (new_state.get("attributes") or {}).get("friendly_name"))
        self.counters["events"] += 1
        for area_id in house.rooms_of(entity_id):
            self.comparator.note_event(area_id, now, "state", entity_id=entity_id,
                                       state=new_state.get("state") if new_state else None)
        for area_id in house.observe(entity_id, obs, now):
            self._evaluate(area_id, now)

    def _connected(self) -> None:
        if self.house is not None and self.session_id is None:
            now = utcnow()
            self.house.resync(now, self._lookup)
            for area_id in self.house.spec.area_ids:
                self._evaluate(area_id, now)
        if self.session_id is None and (self._subscribe_task is None or self._subscribe_task.done()):
            self._attempt = 0
            self._subscribe_task = asyncio.get_running_loop().create_task(self._subscribe_loop())

    def _disconnected(self, reason: str) -> None:
        if self.mode == "authoritative":
            # The integration takes over when the lease runs out; so does this App's own view of it.
            asyncio.get_running_loop().call_later(P.LEASE_SECONDS + 5, self._lease_lapsed)
        if self.session_id is None and self.bridge == "disconnected":
            return
        self.session_id = None
        self.bridge, self.bridge_error = "disconnected", reason
        self.comparator.stop()
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        self._changed()

    def _lease_lapsed(self) -> None:
        if self.mode == "authoritative" and self.session_id is None:
            self._lose("the lease ran out while the bridge was disconnected", P.REASON_LEASE_EXPIRED)

    # ---------------------------------------------------------- the bridge
    async def _subscribe_loop(self) -> None:
        while not self._stopping and self.session_id is None and self.ha.connected:
            self.bridge = "subscribing"
            try:
                self.sub_id = await self.ha.subscribe(
                    "smart_occupancy/engine/subscribe", self._on_bridge, instance_id=self.instance_id,
                    protocol=P.PROTOCOL_VERSION, app_version=self.app_version, core_version=CORE_VERSION,
                    fingerprint=self.fingerprint, resume_token=self._token)
                return
            except HomeAssistantError as err:
                if err.code == "unknown_command":
                    self.bridge, self.bridge_error = "unsupported", (
                        "The installed Smart Occupancy integration has no occupancy bridge (it needs version 0.7.0 "
                        "or newer). The App's engine runs on its own, without comparison.")
                elif err.code == P.REJECT_VERSION:
                    self.bridge, self.bridge_error = "incompatible", str(err).split(": ", 1)[-1]
                else:
                    self.bridge, self.bridge_error = "error", f"{err.code}: {str(err).split(': ', 1)[-1]}"
            except (ConnectionError, TimeoutError) as err:
                self.bridge, self.bridge_error = "disconnected", type(err).__name__
            self._changed()
            delay = RESUBSCRIBE_DELAYS[min(self._attempt, len(RESUBSCRIBE_DELAYS) - 1)]
            self._attempt += 1
            await asyncio.sleep(delay)

    def _on_bridge(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        try:
            if kind == "hello":
                self._on_hello(event)
            elif kind == "result":
                self._on_result(event)
            elif kind == "input":
                self._on_input(event)
            elif kind == "spec":
                self._on_spec(event)
            elif kind == "ownership":
                self._on_ownership(event.get("ownership") or {})
            elif kind == "closed":
                self._disconnected(f"The integration closed the session ({event.get('reason')})")
                if self.ha.connected and not self._stopping:
                    self._connected()
        except Exception as err:  # noqa: BLE001 - a bad message must never break the Home Assistant connection
            self.counters["bridge_errors"] += 1
            _LOGGER.warning("Ignored an invalid bridge message (%s): %s", kind, err)
            if kind == "hello":
                self.bridge, self.bridge_error = "error", f"invalid hello: {err}"
                self.session_id = None
                self._changed()
                # Try a new session later (the integration may have been updated meanwhile).
                asyncio.get_running_loop().call_later(30, self._connected)

    def _on_hello(self, hello: dict[str, Any]) -> None:
        spec = spec_from_wire(hello["spec"])
        house = House(spec)
        for area_id, engine in house.engines.items():
            engine.import_state(hello["state"][area_id])  # an exact copy of the integration engine
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self.house = house
        self.session_id = hello["session_id"]
        self.integration = dict(hello.get("ownership") or {})
        self.bridge, self.bridge_error = "connected", None
        self.counters["sessions"] += 1
        self.results = {}
        self.integration_results = dict(hello.get("results") or {})
        self.result_fired = {}
        now = utcnow()
        lease = hello.get("lease_token")
        if lease and P.valid_id(lease):
            self._token, self.epoch, self.seq = lease, self.integration.get("epoch"), 0
            self.mode = "authoritative"
            self._log_ownership("resumed", epoch=self.epoch)
            self._start_heartbeat()
        else:
            if self.mode == "authoritative":
                self._log_ownership("lost", reason="the integration did not keep the App's ownership")
            self._token, self.epoch = None, None
            self.mode = "shadow"
        house.resync(now, self._lookup)
        self.comparator.start(now, list(spec.area_ids))
        for area_id, wire in self.integration_results.items():
            self.comparator.update(area_id, "integration", wire, now)
        for area_id in spec.area_ids:
            self._evaluate(area_id, now)
        if self.mode == "authoritative":
            self._pending.update(spec.area_ids)
            self._spawn(self._publish())
        self._save_soon()
        self._published_changed(spec.area_ids)
        _LOGGER.info("Occupancy bridge session %s: %d rooms, owner %s (epoch %s), App mode %s",
                     self.session_id[:8], len(spec.rooms), self.integration.get("owner"),
                     self.integration.get("epoch"), self.mode)
        self._changed()

    def _on_result(self, event: dict[str, Any]) -> None:
        area_id, wire = event["area_id"], event["result"]
        if not isinstance(wire, dict):
            raise ValueError("result must be an object")
        self.integration_results[area_id] = wire
        self.result_fired[area_id] = event.get("fired")
        self.comparator.update(area_id, "integration", wire, utcnow())
        self._compare_soon()
        if self.mode != "authoritative":
            self._published_changed([area_id])

    def _on_input(self, event: dict[str, Any]) -> None:
        house = self.house
        if house is None:
            return
        now = utcnow()
        at = parse_time(event.get("at"), "at") or now
        at = min(at, now)
        kind = event.get("kind")
        if kind == "preliminary":
            signals = event.get("signals") or {}
            wanted = {a: frozenset(v) for a, v in signals.items() if isinstance(v, list)}
            for area_id in house.set_preliminary(wanted):
                self.comparator.note_event(area_id, now, "preliminary", signals=sorted(wanted.get(area_id, ())))
                self._evaluate(area_id, now)
        elif kind == "hold":
            area_id = event["area_id"]
            zones = frozenset(z for z in event.get("zones") or [] if isinstance(z, str))
            self.comparator.note_event(area_id, now, "zone_hold", zones=sorted(zones))
            if house.set_holds(area_id, zones, at):
                self._evaluate(area_id, now)
        elif kind == "activity":
            area_id = event["area_id"]
            source = str(event.get("source") or "activity")[:255]
            self.comparator.note_event(area_id, now, "activity", source=source)
            if house.note_activity(area_id, at, source):
                self._evaluate(area_id, now)

    def _on_spec(self, event: dict[str, Any]) -> None:
        spec = spec_from_wire(event["spec"])
        self.integration["config_revision"] = spec.revision()
        house = self.house
        now = utcnow()
        if house is not None and house.spec.same_signals(spec):
            house.reconfigure(spec)  # the same live change the integration applied
            house.resync(now, self._lookup)
            for area_id in spec.area_ids:
                self._evaluate(area_id, now)
            self._flush()  # results refused for the old revision are sent again
        else:
            # Other rooms or signals: the integration reloads and opens a new session (with a new copy).
            self.comparator.stop()
        self._changed()

    def _on_ownership(self, ownership: dict[str, Any]) -> None:
        self.integration = dict(ownership)
        if self.mode == "authoritative" and (ownership.get("owner") != P.OWNER_APP
                                             or ownership.get("epoch") != self.epoch):
            self._lose("the integration changed the owner", ((ownership.get("history") or [{}])[0] or {}).get("reason"))
        self._changed()

    def _lose(self, why: str, code: str | None = None) -> None:
        if self.mode != "authoritative":
            return
        _LOGGER.warning("The App no longer owns occupancy (%s, %s); back to shadow mode", why, code)
        self._log_ownership("lost", reason=code or why)
        self.mode = "shadow" if self.session_id else "standalone"
        self._token, self.epoch = None, None
        self._pending.clear()
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        self._published_changed(set(self.results) | set(self.integration_results))
        self._changed()

    # ----------------------------------------------------------- evaluation
    def _evaluate(self, area_id: str, now: datetime) -> None:
        house = self.house
        if house is None or area_id not in house.engines:
            return
        evaluation = house.evaluate(area_id, now)
        engine = house.engines[area_id]
        self.counters["evaluations"] += 1
        self._schedule(area_id, house.next_deadline(area_id, now), now)
        wire = result_to_wire(evaluation.result, engine.visit_confirmed)
        if wire == self.results.get(area_id):
            return
        self.results[area_id] = wire
        self.comparator.update(area_id, "app", wire, now)
        self._compare_soon()
        if self.mode == "authoritative":
            self._published_changed([area_id])
        if evaluation.transition:
            self.counters["transitions"] += 1
            self._save_soon()
        if self.mode == "authoritative":
            self._pending.add(area_id)
            self._flush()

    def _flush(self) -> None:
        if self.mode == "authoritative" and self._pending:
            self._spawn(self._publish())

    def _schedule(self, area_id: str, when: datetime | None, now: datetime) -> None:
        if handle := self._timers.pop(area_id, None):
            handle.cancel()
        if when is None or self._stopping:
            return
        delay = max(0.0, (when - now).total_seconds())
        self._timers[area_id] = asyncio.get_running_loop().call_later(delay, self._fire, area_id)

    def _fire(self, area_id: str) -> None:
        self._timers.pop(area_id, None)
        self._evaluate(area_id, utcnow())

    # ------------------------------------------------------------ publishing
    async def _publish(self) -> None:
        async with self._publish_lock:
            if self.mode != "authoritative" or not self._pending or self.session_id is None:
                return
            rooms = {a: self.results[a] for a in sorted(self._pending) if a in self.results}
            self._pending.clear()
            self.seq += 1
            try:
                reply = await self.ha.engine_command(
                    "smart_occupancy/engine/publish", session_id=self.session_id, epoch=self.epoch,
                    lease_token=self._token, seq=self.seq, config_revision=self._revision(), results=rooms)
            except HomeAssistantError as err:
                self.counters["publish_refused"] += 1
                self.last_publish_error = {"at": iso(utcnow()), "code": err.code, "detail": str(err)}
                if err.code in LOST_CODES:
                    self._lose("a publish was refused", err.code)
                elif err.code in (P.REJECT_CONFIG, P.REJECT_SEQUENCE):
                    self._pending.update(rooms)  # sent again (with the new configuration / a new sequence)
                else:
                    # The integration cannot use these results: hand control back rather than freeze rooms.
                    _LOGGER.error("The integration refused the App's occupancy results (%s); returning control", err)
                    self._lose("the integration refused the App's results", err.code)
                    self._spawn(self._release_quietly())
                return
            except (ConnectionError, TimeoutError) as err:
                self.counters["publish_failed"] += 1
                self.last_publish_error = {"at": iso(utcnow()), "code": type(err).__name__, "detail": str(err)}
                self._pending.update(rooms)  # sent again with the next publish or heartbeat (or a new session)
                return
            self.counters["published"] += 1
            self.last_publish_at = utcnow()
            self.lease_until = (reply or {}).get("lease_until")

    def _start_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while self.mode == "authoritative" and not self._stopping:
            await asyncio.sleep(P.HEARTBEAT_SECONDS)
            if self.session_id is None:
                continue
            async with self._publish_lock:
                if self.mode != "authoritative":
                    return
                self.seq += 1
                try:
                    reply = await self.ha.engine_command(
                        "smart_occupancy/engine/heartbeat", session_id=self.session_id, epoch=self.epoch,
                        lease_token=self._token, seq=self.seq, config_revision=self._revision())
                except HomeAssistantError as err:
                    self.counters["heartbeat_refused"] += 1
                    if err.code in LOST_CODES:
                        self._heartbeat = None
                        self._lose("a heartbeat was refused", err.code)
                        return
                    continue
                except (ConnectionError, TimeoutError):
                    self.counters["heartbeat_failed"] += 1
                    continue
                self.counters["heartbeats"] += 1
                self.lease_until = (reply or {}).get("lease_until")
            self._flush()

    def _revision(self) -> str | None:
        return self.house.spec.revision() if self.house else None

    # ------------------------------------------------------- handover, rollback
    def readiness(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        own = self.integration
        summary = self.comparator.summary(now)
        totals = summary["totals"]
        last = self.comparator.last_disagreement(now)
        clean_for = (now - last).total_seconds() if last else None
        rooms = summary["rooms"]
        checks = []

        def check(key: str, ok: bool, title: str, detail: str, warn: bool = False) -> None:
            checks.append({"key": key, "status": "ok" if ok else ("warning" if warn else "error"), "title": title,
                           "detail": detail})

        check("bridge", self.bridge == "connected" and self.session_id is not None, "Bridge connected",
              "Connected to the integration's occupancy bridge" if self.bridge == "connected"
              else (self.bridge_error or "Not connected"))
        check("protocol", own.get("protocol") == P.PROTOCOL_VERSION, "Protocol",
              f"Bridge protocol {own.get('protocol')} (the App speaks {P.PROTOCOL_VERSION})")
        same = own.get("fingerprint") == self.fingerprint and own.get("core_version") == CORE_VERSION
        check("engine", same, "Same engine", f"App engine {CORE_VERSION}, integration engine "
              f"{own.get('core_version') or '?'}; fingerprints {'match' if same else 'differ'}")
        passed = bool(self.self_test and self.self_test["passed"])
        check("self_test", passed, "Regression self-test",
              f"{self.self_test['scenarios']} golden scenarios, {self.self_test['transitions']} transitions: "
              f"{'all reproduced' if passed else 'FAILED'}" if self.self_test else "Not run")
        synced = self._revision() is not None and self._revision() == own.get("config_revision")
        check("config", synced, "Configuration in sync",
              f"Revision {self._revision() or '—'} (integration {own.get('config_revision') or '—'})")
        check("owner", own.get("owner") == P.OWNER_INTEGRATION and own.get("state") == P.OWNER_INTEGRATION,
              "Integration is the owner", f"Acknowledged owner: {own.get('state') or 'unknown'}")
        seconds = summary["seconds"]
        check("duration", summary["running"] and seconds >= self.min_shadow_seconds, "Shadow comparison time",
              f"{seconds // 60} of {self.min_shadow_seconds // 60} minutes without interruption")
        check("transitions", totals["matched"] >= self.min_transitions, "Compared transitions",
              f"{totals['matched']} of {self.min_transitions} transitions matched")
        check("clean", last is None or clean_for >= self.min_shadow_seconds, "No disagreement",
              "No disagreement recorded" if last is None else
              f"Last disagreement {round(clean_for // 60)} minutes ago (needs {self.min_shadow_seconds // 60})")
        agree = bool(rooms) and all(r["agree"] is True for r in rooms)
        check("agree_now", agree, "All rooms agree now",
              "Every room has the same decision in both engines" if agree else
              f"{sum(1 for r in rooms if r['agree'] is not True)} room(s) differ or are not known yet")
        deadlines = totals["deadline_differences"]
        check("deadlines", deadlines == 0, "Vacancy deadlines",
              "No deadline difference" if deadlines == 0 else f"{deadlines} deadline difference(s) "
              f"(up to {totals['deadline_max_s']} s)", warn=True)
        ready = all(c["status"] == "ok" for c in checks if c["key"] != "deadlines")
        return {"ready": ready, "checks": checks, "shadow_seconds": seconds, "transitions": totals["matched"],
                "disagreements": totals["disagreements"],
                "thresholds": {"min_shadow_seconds": self.min_shadow_seconds, "min_transitions": self.min_transitions}}

    async def handover(self, user_id: str | None) -> dict[str, Any]:
        """The reviewed handover: only when every readiness check passes; the integration decides."""
        if self.mode == "authoritative":
            raise EngineError("already_owner", "The App already owns occupancy")
        ready = self.readiness()
        if not ready["ready"]:
            failed = [c["title"] for c in ready["checks"] if c["status"] == "error"]
            raise EngineError("not_ready", "Not ready for a handover: " + ", ".join(failed))
        try:
            grant = await self.ha.engine_command(
                "smart_occupancy/engine/claim", session_id=self.session_id,
                expected_epoch=self.integration.get("epoch"), config_revision=self._revision(),
                readiness={k: ready[k] for k in ("ready", "checks", "shadow_seconds", "transitions",
                                                 "disagreements")},
                requested_by=user_id)
        except HomeAssistantError as err:
            raise EngineError(err.code, str(err).split(": ", 1)[-1]) from err
        except (ConnectionError, TimeoutError) as err:
            raise EngineError("not_connected", "Home Assistant is not connected", 503) from err
        if self.house is None:
            raise EngineError("not_connected", "The App's engine has no configuration yet", 503)
        house = House(self.house.spec)
        try:
            for area_id, engine in house.engines.items():
                engine.import_state(grant["state"][area_id])  # exactly the published decisions
        except (StateError, KeyError, TypeError) as err:
            # Nothing was swapped in: give the ownership straight back.
            await self._release_quietly()
            raise EngineError("invalid_state", f"The integration's state could not be copied ({err})", 502) from err
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self.house = house
        self._token, self.epoch, self.seq = grant["lease_token"], grant["epoch"], 0
        self.lease_until = grant.get("lease_until")
        self.mode = "authoritative"
        self.integration.update(owner=P.OWNER_APP, state=P.OWNER_APP, epoch=self.epoch)
        self._log_ownership("handover", epoch=self.epoch, by=user_id)
        now = utcnow()
        self.results = {}
        for area_id in house.spec.area_ids:
            self._evaluate(area_id, now)
        self._pending.update(house.spec.area_ids)
        await self._publish()
        if self.mode != "authoritative":
            raise EngineError("lost", "The integration did not accept the App's first results; it keeps control")
        self._start_heartbeat()
        _LOGGER.warning("Occupancy handover: the App now decides room occupancy (epoch %s)", self.epoch)
        self._changed()
        return {"epoch": self.epoch}

    async def _release_quietly(self) -> None:
        with contextlib.suppress(HomeAssistantError, ConnectionError, TimeoutError):
            await self.ha.engine_command("smart_occupancy/engine/release", reason="the App's results were refused")

    async def release(self, user_id: str | None) -> dict[str, Any]:
        """Return control to the integration (always allowed; harmless when it already owns occupancy)."""
        try:
            reply = await self.ha.engine_command(
                "smart_occupancy/engine/release", reason="Return control to integration (Smart Lighting App)",
                requested_by=user_id)
        except HomeAssistantError as err:
            raise EngineError(err.code, str(err).split(": ", 1)[-1]) from err
        except (ConnectionError, TimeoutError) as err:
            raise EngineError("not_connected", "Home Assistant is not connected; use the service "
                              "smart_occupancy.engine_return_to_integration", 503) from err
        self._lose("returned to the integration", P.REASON_RELEASE)
        if isinstance(reply, dict) and isinstance(reply.get("ownership"), dict):
            self.integration = dict(reply["ownership"])
        self._log_ownership("released", by=user_id)
        self._changed()
        return {"released": bool((reply or {}).get("released"))}

    # --------------------------------------------------------------- reading
    def room_view(self, area_id: str) -> dict[str, Any]:
        return {"area_id": area_id, "app": brief(self.results.get(area_id)),
                "integration": brief(self.integration_results.get(area_id))}

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        own = self.integration
        return {
            "mode": self.mode,
            "acknowledged_owner": own.get("state") or "unknown",
            "epoch": own.get("epoch"),
            "app_epoch": self.epoch,
            "lease_until": own.get("lease_until") if self.mode != "authoritative" else self.lease_until,
            "bridge": self.bridge,
            "bridge_error": self.bridge_error,
            "instance": self.instance_id[:8],
            "session": self.session_id[:8] if self.session_id else None,
            "versions": {"app": self.app_version, "core": CORE_VERSION, "fingerprint": self.fingerprint,
                         "integration": own.get("integration_version"), "integration_core": own.get("core_version"),
                         "integration_fingerprint": own.get("fingerprint"), "protocol": P.PROTOCOL_VERSION,
                         "integration_protocol": own.get("protocol")},
            "config": {"app_revision": self._revision(), "integration_revision": own.get("config_revision"),
                       "in_sync": self._revision() is not None and self._revision() == own.get("config_revision")},
            "self_test": self.self_test,
            "rooms": len(self.house.engines) if self.house else 0,
            "last_publish_at": iso(self.last_publish_at),
            "last_publish_error": self.last_publish_error,
            "counters": dict(self.counters),
            "notices": list(self.notices),
            "app_log": list(self.ownership_log),
            "comparison": self.comparator.summary(now),
            "readiness": self.readiness(now),
            "now": iso(now),
        }


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

