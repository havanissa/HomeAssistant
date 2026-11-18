"""The App's lighting runtime (Phase A4): the integration's lighting controller, in shadow or in control.

* **Shadow** (the integration controls lighting, the default): every bridge
  session starts a fresh controller from an exact copy of the integration's
  configuration and runtime state (``hello``). It follows the integration's
  configuration changes and live option changes, receives the same Home
  Assistant events, and sends nothing: its commands go to the comparison
  (``lighting_compare``) together with the integration's.
* **Owner** (after a reviewed handover the integration acknowledged): the App's
  controller starts from the copy the integration handed over and commands
  lights only through the integration's fenced ``execute`` (current epoch, lease
  token, increasing sequence number). Its configuration and state are stored in
  ``/data/lighting/store`` and pushed to the integration's mirror whenever they
  are saved; its occupancy inputs go to the integration's engine. A refusal that
  means "not the owner" stops it at once.
* **Restarts and reconnects** never replay anything: after a reconnect the owner
  resumes with its lease token and restarts its controller from its own stored
  state (start applies nothing); a new App process is a new instance and the
  integration takes lighting back; the App then shadows again until an
  administrator hands over again.
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
import shutil
from typing import Any

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError as HostError

from occupancy_core import fingerprint as core_fingerprint
from smart_occupancy import lighting_sync as S
from smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint as runtime_fingerprint

from .ha import HomeAssistantClient, HomeAssistantError
from .lighting_compare import LightingComparator, iso
from .lighting_host import LIGHTING_STATE_STORE_KEY, LIGHTING_STORE_KEY, AppBackend, LightingInstance, room_result
from .lighting_scripts import AutomationEntity, ScriptEntity

_LOGGER = logging.getLogger(__name__)

MIN_SHADOW_SECONDS = 2 * 3600
MIN_COMMANDS = 10
PUBLISH_DELAY = 2.0
VIEW_DELAY = 2.0
RESUBSCRIBE_DELAYS = (2, 5, 15, 30, 60)
OWNERSHIP_LOG = 50
BACKUPS = 20


class LightingError(Exception):
    """A handover, rollback or command request was refused (``code`` is stable)."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> datetime:
    return datetime.now(UTC)


def _code(err: HomeAssistantError) -> str:
    return err.code


def _detail(err: Exception) -> str:
    return str(err).split(": ", 1)[-1]


class LightingRuntime:
    def __init__(self, ha: HomeAssistantClient, occupancy: Any, data_dir: Path | None, *, app_version: str,
                 min_shadow_seconds: int = MIN_SHADOW_SECONDS, min_commands: int = MIN_COMMANDS) -> None:
        self.ha = ha
        self.occupancy = occupancy
        self.dir = data_dir / "lighting" if data_dir else None
        self.app_version = app_version
        self.min_shadow_seconds = min_shadow_seconds
        self.min_commands = min_commands
        self.instance_id = secrets.token_hex(16)  # new for every App process: it never resumes an old ownership
        self.fingerprint: str | None = None
        self.core_fingerprint: str | None = None
        self.mode = "starting"  # starting | standalone | shadow | owner
        self.bridge = "disconnected"  # disconnected | subscribing | connected | unsupported | incompatible | error
        self.bridge_error: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.integration: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.integration_revision: str | None = None
        self.instance: LightingInstance | None = None
        self.epoch: int | None = None
        self._token: str | None = None
        self.seq = 0
        self.lease_until: str | None = None
        self.comparator = LightingComparator()
        self.counters: Counter[str] = Counter()
        self.last_error: dict[str, Any] | None = None
        self.notices: list[dict[str, str]] = []
        self.ownership_log: deque[dict[str, Any]] = deque(maxlen=OWNERSHIP_LOG)
        self.automations: list[AutomationEntity] = []
        self.scripts: list[ScriptEntity] = []
        self._listeners: list[Callable[[str], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeat: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None
        self._publish_handle: asyncio.TimerHandle | None = None
        self._view_handle: asyncio.TimerHandle | None = None
        self._dirty: set[str] = set()
        self._last_view: dict[str, Any] | None = None
        self._backlog: list[dict[str, Any]] | None = None  # events that arrive while a hello is processed
        self._switching = False
        self._attempt = 0
        self._stopping = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.fingerprint = await asyncio.to_thread(runtime_fingerprint)
        self.core_fingerprint = await asyncio.to_thread(core_fingerprint)
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = "standalone"
        self._unsubs.append(self.ha.add_listener(self._on_ha))
        self._unsubs.append(self.occupancy.add_result_listener(self._on_occupancy))
        if self.ha.connected:
            self._connected()

    async def stop(self) -> None:
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for handle in (self._publish_handle, self._view_handle):
            if handle:
                handle.cancel()
        for task in (self._heartbeat, self._subscribe_task, *self._tasks):
            if task:
                task.cancel()
        await self._stop_instance(flush=True)

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self, what: str = "lighting") -> None:
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Lighting listener failed")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _log(self, event: str, **data: Any) -> None:
        self.ownership_log.appendleft({"at": iso(utcnow()), "event": event, **data})

    def _notice(self, code: str, text: str) -> None:
        self.notices = [n for n in self.notices if n["code"] != code][-9:] + [{"code": code, "text": text}]

    # --------------------------------------------------------------- events
    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._disconnected(str((payload or {}).get("last_error") or "disconnected"))
        elif kind == "state":
            entity_id, new_state = payload
            if self.instance is not None:
                self.instance.state_changed(entity_id, new_state)
            if entity_id.startswith(("automation.", "script.")) and (new_state is None or
                                                                       entity_id not in self._known_scripts()):
                self._spawn(self._load_automations())
        elif kind == "registries":
            if self.instance is not None:
                self.instance.registries_changed()
        elif kind == "registry_event":
            self._on_registry_event(payload or {})
        elif kind == "event":
            event_type = (payload or {}).get("event_type")
            if event_type in ("automation_reloaded", "script_reloaded"):
                self._spawn(self._load_automations())
            elif event_type == "homeassistant_started" and self.instance is not None:
                self.instance.ha_started()

    def _known_scripts(self) -> set[str]:
        return {e.entity_id for e in (*self.automations, *self.scripts)}

    def _on_occupancy(self, area_id: str) -> None:
        if self.instance is not None:
            self.instance.occupancy_changed(area_id)

    def _on_registry_event(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        if event.get("event_type") != "entity_registry_updated" or data.get("action") != "update" or \
                "old_entity_id" not in data or self.mode != "owner" or self.instance is None:
            return
        from smart_occupancy.lighting_config import rename_entity  # noqa: PLC0415

        ctrl = self.instance.controller
        if ctrl is not None and (lighting := rename_entity(ctrl.store.config, data["old_entity_id"],
                                                           data["entity_id"])) is not None:
            _LOGGER.info("entity_renamed old=%s new=%s (lighting configuration updated)", data["old_entity_id"],
                         data["entity_id"])
            ctrl.store.async_set(lighting)

    async def _load_automations(self) -> None:
        """Raw configuration of every automation and script (conflict detection), then tell the controller."""
        automations, scripts = [], []
        for state in list(self.ha.states.values()):
            entity_id = state.get("entity_id", "")
            domain = entity_id.split(".", 1)[0]
            if domain not in ("automation", "script"):
                continue
            try:
                reply = await self.ha.command(f"{domain}/config", entity_id=entity_id)
            except (HomeAssistantError, ConnectionError, TimeoutError):
                reply = None
            raw = (reply or {}).get("config") if isinstance(reply, dict) else None
            (automations if domain == "automation" else scripts).append(
                (AutomationEntity if domain == "automation" else ScriptEntity)(entity_id, raw if isinstance(raw, dict)
                                                                                else None))
        self.automations, self.scripts = automations, scripts
        if self.instance is not None:
            self.instance.automations_changed(automations, scripts)

    def _connected(self) -> None:
        if self._subscribe_task is None or self._subscribe_task.done():
            self._attempt = 0
            self._subscribe_task = asyncio.get_running_loop().create_task(self._subscribe_loop())

    def _disconnected(self, reason: str) -> None:
        self.session_id = None
        self.sub_id = None
        self.bridge, self.bridge_error = "disconnected", reason
        self.comparator.stop()
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        # Events of the gap are lost: the instance is rebuilt from a fresh picture on the next session.
        self._spawn(self._stop_instance(flush=self.mode == "owner"))
        self._changed()

    async def _subscribe_loop(self) -> None:
        while not self._stopping and self.ha.connected and self.session_id is None:
            self.bridge = "subscribing"
            try:
                await self._load_automations()
                self.sub_id = await self.ha.subscribe(
                    "smart_occupancy/lighting/bridge/subscribe", self._on_bridge,
                    instance_id=self.instance_id, protocol=S.PROTOCOL_VERSION, app_version=self.app_version,
                    runtime_version=RUNTIME_VERSION, fingerprint=self.fingerprint,
                    core_fingerprint=self.core_fingerprint, resume_token=self._token)
                return
            except HomeAssistantError as err:
                if err.code in ("unknown_command", "not_found"):
                    self.bridge, self.bridge_error = "unsupported", (
                        "The integration does not offer the lighting bridge (it needs version 0.8.0 or later)")
                elif err.code == S.REJECT_VERSION:
                    self.bridge, self.bridge_error = "incompatible", _detail(err)
                else:
                    self.bridge, self.bridge_error = "error", f"{err.code}: {_detail(err)}"
            except (ConnectionError, TimeoutError) as err:
                self.bridge, self.bridge_error = "disconnected", type(err).__name__
            self._changed()
            await asyncio.sleep(RESUBSCRIBE_DELAYS[min(self._attempt, len(RESUBSCRIBE_DELAYS) - 1)])
            self._attempt += 1

    async def _resubscribe(self) -> None:
        """A fresh bridge session (a new copy of the integration's controller)."""
        if self.sub_id is not None:
            await self.ha.unsubscribe(self.sub_id)
        self.sub_id = None
        self.session_id = None
        self._connected()

    def _on_bridge(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "hello":
            self._backlog = []
            self._spawn(self._on_hello(event))
            return
        if self._backlog is not None:
            self._backlog.append(event)  # replayed in order once the new controller runs
            return
        self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        try:
            if kind == "config":
                self._on_config(event)
            elif kind == "options":
                self._on_options(event)
            elif kind == "command":
                self.comparator.command("integration", event.get("command") or {})
            elif kind == "decision":
                self.comparator.decision("integration", str(event.get("subject")), event.get("decision") or {})
            elif kind == "ownership":
                self._on_ownership(event.get("ownership") or {})
            elif kind == "closed":
                self.session_id = None
                self.sub_id = None
                self.bridge, self.bridge_error = "disconnected", "the integration closed the session"
                self.comparator.stop()
                self._spawn(self._stop_instance(flush=self.mode == "owner"))
                self._connected()
        except Exception as err:  # noqa: BLE001 - a bad message never stops the runtime
            _LOGGER.warning("Lighting bridge message %s not usable: %s", kind, err)
        self._changed()

    async def _on_hello(self, hello: dict[str, Any]) -> None:
        try:
            await self._hello(hello)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Lighting bridge session could not start")
            self.bridge, self.bridge_error = "error", f"invalid hello: {err}"
        finally:
            backlog, self._backlog = self._backlog or [], None
            for event in backlog:
                self._dispatch(event)
            self._changed()

    async def _hello(self, hello: dict[str, Any]) -> None:
        self.session_id = hello.get("session_id")
        self.integration = dict(hello.get("ownership") or {})
        self.options = dict(hello.get("options") or {})
        self.integration_revision = hello.get("config_revision")
        self.bridge, self.bridge_error = "connected", None
        self.counters["sessions"] += 1
        lease = hello.get("lease_token")
        await self._stop_instance(flush=self.mode == "owner")
        if lease and S.valid_id(lease):
            self._token, self.epoch, self.seq = lease, self.integration.get("epoch"), 0
            self.mode = "owner"
            self._log("resumed", epoch=self.epoch)
            await self._start_owner(config=None, state=None, recent=None)
        else:
            if self.mode == "owner":
                self._log("lost", reason="the integration did not keep the App's control")
                self._notice("lost", "Lighting control returned to the integration while the App was away.")
            self._token, self.epoch = None, None
            self.mode = "shadow"
            await self._start_shadow(hello)
        self._changed()

    def _on_config(self, event: dict[str, Any]) -> None:
        self.integration_revision = event.get("config_revision")
        inst = self.instance
        if self.mode != "shadow" or inst is None or inst.controller is None:
            return
        from smart_occupancy.lighting_config import LightingConfig  # noqa: PLC0415

        inst.controller.store.async_set(LightingConfig.from_dict(event.get("config") or {}))

    def _on_options(self, event: dict[str, Any]) -> None:
        self.options = dict(event.get("options") or {})
        inst = self.instance
        if inst is None or inst.controller is None:
            return
        inst.coordinator.set_options(self.options)  # a live change (presence settings), as the integration's
        inst.controller.async_presence_changed()

    def _on_ownership(self, ownership: dict[str, Any]) -> None:
        self.integration = dict(ownership)
        if self.mode == "owner" and (ownership.get("owner") != S.OWNER_APP or ownership.get("epoch") != self.epoch):
            self._spawn(self._lose("the integration changed the owner"))

    # ------------------------------------------------------------ instances
    def _published(self, area_id: str) -> Any:
        return room_result(self.occupancy, area_id)

    def _backend(self, owner: bool) -> AppBackend:
        if owner:
            return AppBackend(self.ha, execute=self._execute, update_options=self._update_options,
                              store_saved=self._store_saved)
        return AppBackend(self.ha, execute=None)

    async def _start_shadow(self, hello: dict[str, Any]) -> None:
        inst = LightingInstance(self.ha, role="shadow", options=self.options, config=hello.get("config") or {},
                                state=hello.get("state"), published=self._published, send_input=lambda *_: None,
                                backend=self._backend(False), storage_dir=None, automations=self.automations,
                                scripts=self.scripts)
        await self._run_instance(inst, hello.get("recent"))
        ctrl = inst.controller
        ctrl.command_observer = lambda record: self.comparator.command("app", record)
        ctrl.decisions.observer = lambda subject, d: self.comparator.decision("app", subject, d)
        self.comparator.start(utcnow())

    async def _start_owner(self, *, config: dict[str, Any] | None, state: dict[str, Any] | None,
                           recent: dict[str, Any] | None) -> None:
        """The App's controller in control: from the handed-over copy, or (resume) from its own store."""
        store = self.dir / "store" if self.dir else None
        inst = LightingInstance(self.ha, role="owner", options=self.options, config=config, state=state,
                                published=self._published, send_input=self._send_input,
                                backend=self._backend(True), storage_dir=store, automations=self.automations,
                                scripts=self.scripts)
        await self._run_instance(inst, recent)
        self.comparator.stop()
        inst.occupancy.resend()
        self._dirty.update({"config", "state"})
        self._schedule_publish(0)
        self._schedule_view(0)
        from homeassistant.helpers.dispatcher import async_dispatcher_connect  # noqa: PLC0415
        from smart_occupancy.const import SIGNAL_LIGHTING_UPDATED  # noqa: PLC0415

        async_dispatcher_connect(inst.hass, SIGNAL_LIGHTING_UPDATED, lambda: self._schedule_view(VIEW_DELAY))
        self._start_heartbeat()

    async def _run_instance(self, inst: LightingInstance, recent: Any) -> None:
        self.instance = inst
        await inst.async_start()
        if recent:
            inst.controller.note_recent_commands(recent)

    async def _stop_instance(self, *, flush: bool) -> None:
        inst, self.instance = self.instance, None
        if inst is not None:
            await inst.async_stop(flush=flush)

    # ---------------------------------------------------------------- owner
    def _owner_params(self) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session_id, "epoch": self.epoch, "lease_token": self._token, "seq": self.seq}

    async def _owner_command(self, command: str, **params: Any) -> Any:
        if self.mode != "owner" or self.session_id is None or self._token is None:
            raise LightingError(S.REJECT_NOT_OWNER, "The App does not control lighting")
        try:
            return await self.ha.lighting_command(command, **self._owner_params(), **params)
        except HomeAssistantError as err:
            if err.code in S.LOST_CODES:
                self._spawn(self._lose(f"{command.rsplit('/', 1)[-1]} refused: {err.code}"))
            raise

    async def _execute(self, domain: str, service: str, data: dict[str, Any], context: Context) -> None:
        """The owner controller's only way to a device (the integration fences and allowlists it)."""
        self.counters["commands"] += 1
        try:
            await self._owner_command("smart_occupancy/lighting/bridge/execute", domain=domain, service=service,
                                      data=data, context_id=context.id)
        except LightingError as err:
            raise HostError(str(err)) from err
        except HomeAssistantError as err:
            self.counters["command_errors"] += 1
            self.last_error = {"at": iso(utcnow()), "code": err.code, "detail": _detail(err)}
            if err.code == S.REJECT_NOT_ALLOWED:
                _LOGGER.warning("The integration refused a lighting command: %s", _detail(err))
            raise HostError(_detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            self.counters["command_errors"] += 1
            raise HostError("Home Assistant is not connected") from err

    def _send_input(self, kind: str, params: dict[str, Any]) -> None:
        if self.mode == "owner":
            self._spawn(self._send_input_now(kind, params))

    async def _send_input_now(self, kind: str, params: dict[str, Any]) -> None:
        with contextlib.suppress(LightingError, HomeAssistantError, ConnectionError, TimeoutError):
            await self._owner_command("smart_occupancy/lighting/bridge/inputs", kind=kind, **params)

    async def _update_options(self, options: dict[str, Any]) -> None:
        """A lighting command of the App changed the integration's options (light types or the mood source)."""
        changes = {k: options[k] for k in ("mood", "categories") if options.get(k) != self.options.get(k)}
        if not changes:
            return
        await self._owner_command("smart_occupancy/lighting/bridge/options",
                                  options_revision=S.config_revision(self.options), **changes)

    def _store_saved(self, key: str, data: Any) -> None:
        if key == LIGHTING_STORE_KEY:
            self._dirty.add("config")
        elif key == LIGHTING_STATE_STORE_KEY:
            self._dirty.add("state")
        else:
            return
        self._schedule_publish(PUBLISH_DELAY)

    def _schedule_publish(self, delay: float) -> None:
        if self._publish_handle is None:
            self._publish_handle = asyncio.get_running_loop().call_later(
                delay, lambda: self._spawn(self._publish()))

    def _schedule_view(self, delay: float) -> None:
        if self._view_handle is None:
            self._view_handle = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self._publish_view()))

    def config_doc(self) -> dict[str, Any] | None:
        inst = self.instance
        return inst.controller.lighting.as_dict() if inst is not None and inst.controller is not None else None

    async def _publish(self) -> None:
        self._publish_handle = None
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.controller is None or not self._dirty:
            return
        dirty, self._dirty = self._dirty, set()
        params: dict[str, Any] = {}
        if "config" in dirty:
            doc = inst.controller.lighting.as_dict()
            params.update(config=doc, config_revision=S.config_revision(doc))
        if "state" in dirty:
            params["state"] = inst.controller.export_state()
        try:
            await self._owner_command("smart_occupancy/lighting/bridge/publish", **params)
            self.counters["publishes"] += 1
        except (LightingError, HomeAssistantError, ConnectionError, TimeoutError) as err:
            self._dirty |= dirty
            self.last_error = {"at": iso(utcnow()), "code": getattr(err, "code", type(err).__name__),
                               "detail": str(err)}
            if self.mode == "owner":
                self._schedule_publish(10)

    def room_views(self) -> dict[str, Any]:
        inst = self.instance
        if inst is None or inst.controller is None:
            return {}
        ctrl = inst.controller
        out: dict[str, Any] = {}
        for area_id in sorted(ctrl.config.enabled_rooms):
            rt = ctrl.runtime.get(area_id)
            activation = rt.activation if rt else None
            out[area_id] = {
                "active": ctrl.room_active(area_id),
                "diagnostics": _jsonable(ctrl.room_diagnostics(area_id)),
                "daylight": _jsonable(ctrl.daylight.status(area_id).summary()),
                "activation": {"at": rt.last_activation_at if rt else None,
                               "text": activation.get("text") if activation else None},
            }
        return out

    async def _publish_view(self) -> None:
        self._view_handle = None
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.controller is None:
            return
        rooms = self.room_views()
        manual = inst.controller.manual_mode
        view = {"rooms": rooms, "manual_mode": manual}
        if view == self._last_view:
            return
        try:
            await self._owner_command("smart_occupancy/lighting/bridge/publish", rooms=rooms, manual_mode=manual)
            self._last_view = view
        except (LightingError, HomeAssistantError, ConnectionError, TimeoutError):
            if self.mode == "owner":
                self._schedule_view(10)

    def _start_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while self.mode == "owner" and not self._stopping:
            await asyncio.sleep(S.HEARTBEAT_SECONDS)
            if self.mode != "owner":
                return
            try:
                reply = await self._owner_command("smart_occupancy/lighting/bridge/heartbeat")
                self.lease_until = (reply or {}).get("lease_until")
            except (LightingError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug("lighting heartbeat failed: %s", err)

    async def _lose(self, why: str) -> None:
        """No longer the owner: stop at once (nothing more is sent), then shadow again from a fresh copy."""
        if self.mode != "owner":
            return
        _LOGGER.warning("The App no longer controls lighting (%s); back to shadow mode", why)
        self._log("lost", reason=why)
        self.mode = "shadow"
        self._token, self.epoch = None, None
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        await self._stop_instance(flush=True)
        if self.ha.connected:
            await self._resubscribe()
        self._changed()

    # -------------------------------------------------------- handover, return
    def readiness(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        report = self.comparator.report(now)
        checks: list[dict[str, Any]] = []

        def check(key: str, ok: bool, title: str, detail: str) -> None:
            checks.append({"key": key, "status": "ok" if ok else "error", "title": title, "detail": detail})

        inst = self.instance
        integ = self.integration
        app_revision = S.config_revision(inst.controller.lighting.as_dict()) if inst and inst.controller else None
        rooms = sorted(inst.controller.config.enabled_rooms) if inst and inst.controller else []
        missing = [a for a in rooms if self._published(a) is None]
        check("bridge", self.bridge == "connected", "Lighting bridge connected", self.bridge_error or self.bridge)
        check("fingerprint", bool(self.fingerprint) and self.fingerprint == integ.get("fingerprint"),
              "Same lighting code", "The App and the integration run identical lighting modules"
              if self.fingerprint == integ.get("fingerprint") else "Update the App and the integration together")
        check("core", bool(self.core_fingerprint) and self.core_fingerprint == integ.get("core_fingerprint"),
              "Same occupancy engine", "Identical occupancy engine code on both sides")
        check("config", app_revision is not None and app_revision == self.integration_revision,
              "Configuration in sync", f"App {app_revision or '-'}, integration {self.integration_revision or '-'}")
        check("owner", integ.get("owner") == S.OWNER_INTEGRATION and integ.get("state") == S.OWNER_INTEGRATION,
              "The integration controls lighting", f"owner {integ.get('owner') or '-'}")
        check("occupancy", not missing, "Occupancy available", "Every room has published occupancy"
              if not missing else f"No occupancy yet for {', '.join(missing)}")
        check("duration", report["seconds"] >= self.min_shadow_seconds, "Compared long enough",
              f"{report['seconds'] // 60} of {self.min_shadow_seconds // 60} min")
        check("commands", report["commands"]["matched"] >= self.min_commands, "Enough matching commands",
              f"{report['commands']['matched']} of {self.min_commands}")
        check("mismatches", report["commands"]["mismatches"] == 0, "No command mismatch",
              f"{report['commands']['mismatches']} since the comparison started")
        ready = all(c["status"] == "ok" for c in checks) and self.mode == "shadow"
        return {"ready": ready, "checks": checks, "summary": {
            "seconds": report["seconds"], "matched": report["commands"]["matched"],
            "mismatches": report["commands"]["mismatches"], "decision_differences": report["decisions"]["differences"]},
            "thresholds": {"min_shadow_seconds": self.min_shadow_seconds, "min_commands": self.min_commands}}

    async def handover(self, user_id: str | None) -> dict[str, Any]:
        """The reviewed handover: only when every readiness check passes; the integration decides."""
        if self.mode == "owner":
            raise LightingError("already_owner", "The App already controls lighting")
        if self._switching:
            raise LightingError("busy", "A change of control is in progress")
        ready = self.readiness()
        if not ready["ready"]:
            failed = [c["title"] for c in ready["checks"] if c["status"] == "error"]
            raise LightingError("not_ready", "Not ready for a handover: " + ", ".join(failed))
        self._switching = True
        try:
            try:
                grant = await self.ha.lighting_command(
                    "smart_occupancy/lighting/bridge/claim", session_id=self.session_id,
                    expected_epoch=self.integration.get("epoch"), config_revision=self.integration_revision,
                    readiness={k: ready[k] for k in ("ready", "checks", "summary")}, requested_by=user_id)
            except HomeAssistantError as err:
                raise LightingError(err.code, _detail(err)) from err
            except (ConnectionError, TimeoutError) as err:
                raise LightingError("not_connected", "Home Assistant is not connected", 503) from err
            self._backup(grant, "handover")
            await self._stop_instance(flush=False)
            self._token, self.epoch, self.seq = grant["lease_token"], grant["epoch"], 0
            self.lease_until = grant.get("lease_until")
            self.mode = "owner"
            self.integration.update(owner=S.OWNER_APP, state=S.OWNER_APP, epoch=self.epoch)
            self._log("handover", epoch=self.epoch, by=user_id)
            store = self.dir / "store" if self.dir else None
            if store is not None and store.exists():
                await asyncio.to_thread(shutil.rmtree, store)
            await self._start_owner(config=grant["config"], state=grant["state"], recent=grant.get("recent"))
            check = self.verify_copy(grant["config"])
        finally:
            self._switching = False
        _LOGGER.warning("Lighting handover: the App now controls lighting (epoch %s)", self.epoch)
        self._changed()
        return {"epoch": self.epoch, "copy": check}

    def verify_copy(self, source: dict[str, Any]) -> dict[str, Any]:
        """Source and destination configuration, key by key (the App's controller loaded what it was given)."""
        doc = self.config_doc() or {}
        differences = sorted(k for k in set(source) | set(doc) if source.get(k) != doc.get(k))
        return {"source_revision": S.config_revision(source), "destination_revision": S.config_revision(doc),
                "identical": not differences, "differences": differences[:50],
                "rooms": len(doc.get("rooms") or {}), "keys": len(doc)}

    def _backup(self, data: dict[str, Any], reason: str) -> None:
        if self.dir is None:
            return
        folder = self.dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        payload = {"schema": 1, "reason": reason, "at": iso(utcnow()), "config": data.get("config"),
                   "config_revision": data.get("config_revision"), "state": data.get("state")}
        path = folder / f"lighting-{stamp}-{reason}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)
        for old in sorted(folder.glob("lighting-*.json"))[:-BACKUPS]:
            old.unlink(missing_ok=True)

    def backups(self) -> list[dict[str, Any]]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not folder.is_dir():
            return []
        return [{"id": p.stem, "size": p.stat().st_size} for p in sorted(folder.glob("lighting-*.json"), reverse=True)]

    def export(self) -> dict[str, Any]:
        """The current lighting configuration and state (the rollback / backup export)."""
        inst = self.instance
        ctrl = inst.controller if inst else None
        doc = ctrl.lighting.as_dict() if ctrl else None
        return {"schema": 1, "at": iso(utcnow()), "mode": self.mode, "config": doc,
                "config_revision": S.config_revision(doc) if doc is not None else None,
                "state": ctrl.export_state() if ctrl else None, "options": self.options}

    async def release(self, user_id: str | None) -> dict[str, Any]:
        """Return control to the integration with the App's final configuration and state."""
        if self.mode != "owner" or self.instance is None:
            return await self.return_control(user_id)
        if self._switching:
            raise LightingError("busy", "A change of control is in progress")
        self._switching = True
        try:
            inst = self.instance
            ctrl = inst.controller
            ctrl.fenced = True  # nothing new is sent from here on
            await ctrl.async_drain(S.DRAIN_SECONDS)
            final = self.export()
            self._backup(final, "release")
            params = {"config": final["config"], "config_revision": final["config_revision"],
                      "state": final["state"], "rooms": {}, "detail": "returned from the App"}
            try:
                reply = await self._owner_command("smart_occupancy/lighting/bridge/release", **params)
            except (LightingError, HomeAssistantError) as err:
                ctrl.fenced = False
                raise LightingError(getattr(err, "code", "failed"), _detail(err)) from err
            self.mode = "shadow"
            self._token, self.epoch = None, None
            if self._heartbeat:
                self._heartbeat.cancel()
                self._heartbeat = None
            await self._stop_instance(flush=True)
            self.integration = dict((reply or {}).get("ownership") or {})
            self._log("released", by=user_id)
        finally:
            self._switching = False
        await self._resubscribe()
        self._changed()
        return {"ownership": self.integration}

    async def return_control(self, user_id: str | None) -> dict[str, Any]:
        """Any time: the integration takes lighting back from its mirror (also when the App is not the owner)."""
        if self.mode == "owner" and self.instance is not None:
            self.instance.controller.fenced = True
            await self.instance.controller.async_drain(S.DRAIN_SECONDS)
        try:
            reply = await self.ha.lighting_command("smart_occupancy/lighting/bridge/return",
                                                   reason="returned from the App")
        except HomeAssistantError as err:
            raise LightingError(err.code, _detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            raise LightingError("not_connected", "Home Assistant is not connected", 503) from err
        self._log("returned", by=user_id)
        if self.mode == "owner":
            await self._lose("returned to the integration")
        self.integration = dict((reply or {}).get("ownership") or self.integration)
        self._changed()
        return {"ownership": self.integration}

    # ---------------------------------------------------------------- status
    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        inst = self.instance
        integ = self.integration
        return {
            "mode": self.mode, "bridge": self.bridge, "bridge_error": self.bridge_error,
            "instance_id": self.instance_id[:8], "session_id": (self.session_id or "")[:8] or None,
            "app_version": self.app_version, "runtime_version": RUNTIME_VERSION,
            "fingerprint": self.fingerprint, "core_fingerprint": self.core_fingerprint,
            "integration": {k: integ.get(k) for k in (
                "owner", "state", "epoch", "integration_version", "fingerprint", "core_fingerprint", "lease_until",
                "changed_at", "changed_by", "config_revision", "controller", "executed", "execute_errors",
                "rejections", "history")},
            "epoch": self.epoch, "lease_until": self.lease_until,
            "config_revision": S.config_revision(inst.controller.lighting.as_dict())
            if inst and inst.controller else None,
            "integration_revision": self.integration_revision,
            "running": bool(inst and inst.started), "counters": dict(self.counters), "last_error": self.last_error,
            "notices": self.notices, "ownership_log": list(self.ownership_log),
            "comparison": self.comparator.report(now), "readiness": self.readiness(now),
        }

    @property
    def controller(self) -> Any:
        inst = self.instance
        return inst.controller if inst is not None else None


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))
