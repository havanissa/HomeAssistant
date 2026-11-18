"""The App's curtain runtime (Phase A5): the integration's curtain manager, in shadow or in control.

* **Shadow** (the integration controls the curtains, the default): every bridge
  session starts a fresh manager from an exact copy of the integration's
  configuration and runtime state (``hello``). It follows the integration's
  configuration changes, receives the same Home Assistant events and the same
  published occupancy, and sends nothing: its cover commands go to the comparison
  (``curtains_compare``) together with the integration's, as do its decisions.
* **Owner** (after a reviewed handover the integration acknowledged): the App's
  manager starts from the copy the integration handed over and moves a cover only
  through the integration's fenced ``execute`` (current epoch, lease token,
  increasing sequence number, a cover service of a configured cover). Its
  configuration and state are stored in ``/data/curtains/store`` and pushed to the
  integration's mirror whenever they are saved. A refusal that means "not the
  owner" stops it at once.
* **Restarts and reconnects** never replay a movement: after a reconnect the owner
  resumes with its lease token and restarts its manager from its own stored state
  (an operation that was running is interrupted, its covers' reports stay "ours";
  the start-up settle period passes before any rule sends); a new App process is a
  new instance and the integration takes the curtains back; the App then shadows
  again until an administrator hands over again.
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
from smart_occupancy import curtain_sync as S
from smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint as runtime_fingerprint

from .curtains_compare import CurtainComparator, iso
from .curtains_host import CURTAINS_STATE_STORE_KEY, CURTAINS_STORE_KEY, CurtainInstance
from .ha import HomeAssistantClient, HomeAssistantError
from .lighting_host import AppBackend, room_result
from .lighting_scripts import AutomationEntity, ScriptEntity

_LOGGER = logging.getLogger(__name__)

MIN_SHADOW_SECONDS = 2 * 3600
MIN_DECISIONS = 5
PUBLISH_DELAY = 2.0
MODELS_DELAY = 0.5
RESUBSCRIBE_DELAYS = (2, 5, 15, 30, 60)
OWNERSHIP_LOG = 50
BACKUPS = 20
EXPORT_SCHEMA = 1


class CurtainsError(Exception):
    """A handover, rollback, import or command request was refused (``code`` is stable)."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> datetime:
    return datetime.now(UTC)


def _detail(err: Exception) -> str:
    return str(err).split(": ", 1)[-1]


class CurtainRuntime:
    def __init__(self, ha: HomeAssistantClient, occupancy: Any, data_dir: Path | None, *, app_version: str,
                 min_shadow_seconds: int = MIN_SHADOW_SECONDS, min_decisions: int = MIN_DECISIONS) -> None:
        self.ha = ha
        self.occupancy = occupancy
        self.dir = data_dir / "curtains" if data_dir else None
        self.app_version = app_version
        self.min_shadow_seconds = min_shadow_seconds
        self.min_decisions = min_decisions
        self.instance_id = secrets.token_hex(16)
        self.fingerprint: str | None = None
        self.core_fingerprint: str | None = None
        self.mode = "starting"  # starting | standalone | shadow | owner
        self.bridge = "disconnected"  # disconnected | subscribing | connected | unsupported | incompatible | error
        self.bridge_error: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.integration: dict[str, Any] = {}
        self.integration_revision: str | None = None
        self.instance: CurtainInstance | None = None
        self.epoch: int | None = None
        self._token: str | None = None
        self.seq = 0
        self.lease_until: str | None = None
        self.comparator = CurtainComparator()
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
        self._dirty: set[str] = set()
        self._models_handle: asyncio.TimerHandle | None = None
        self._backlog: list[dict[str, Any]] | None = None
        self._switching = False
        self._attempt = 0
        self._stopping = False

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.fingerprint = await asyncio.to_thread(runtime_fingerprint)
        self.core_fingerprint = await asyncio.to_thread(core_fingerprint)
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
        for handle in (self._publish_handle, self._models_handle):
            if handle:
                handle.cancel()
        for task in (self._heartbeat, self._subscribe_task, *self._tasks):
            if task:
                task.cancel()
        await self._stop_instance(flush=True)

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self, what: str = "curtains") -> None:
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Curtains listener failed")

    def _models_changed(self) -> None:
        if self._models_handle is None:
            self._models_handle = asyncio.get_running_loop().call_later(MODELS_DELAY, self._models_fire)

    def _models_fire(self) -> None:
        self._models_handle = None
        self._changed("models")

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
        elif kind == "reported":
            if self.instance is not None:
                self.instance.state_reported(*payload)
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
            fired = getattr(self.occupancy, "result_fired", {}).get(area_id)
            self.instance.occupancy_changed(area_id, fired)

    def _on_registry_event(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        if event.get("event_type") != "entity_registry_updated" or data.get("action") != "update" or \
                "old_entity_id" not in data or self.mode != "owner" or self.instance is None:
            return
        mgr = self.instance.manager
        if mgr is not None:
            mgr.rename_entity(data["old_entity_id"], data["entity_id"])

    async def _load_automations(self) -> None:
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
        self._spawn(self._stop_instance(flush=self.mode == "owner"))
        self._changed()

    async def _subscribe_loop(self) -> None:
        while not self._stopping and self.ha.connected and self.session_id is None:
            self.bridge = "subscribing"
            try:
                await self._load_automations()
                self.sub_id = await self.ha.subscribe(
                    "smart_occupancy/curtains/bridge/subscribe", self._on_bridge,
                    instance_id=self.instance_id, protocol=S.PROTOCOL_VERSION, app_version=self.app_version,
                    runtime_version=RUNTIME_VERSION, fingerprint=self.fingerprint,
                    core_fingerprint=self.core_fingerprint, resume_token=self._token)
                return
            except HomeAssistantError as err:
                if err.code in ("unknown_command", "not_found"):
                    self.bridge, self.bridge_error = "unsupported", (
                        "The integration does not offer the curtain bridge (it needs version 0.9.0 or later)")
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
            self._backlog.append(event)
            return
        self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        try:
            if kind == "config":
                self._on_config(event)
            elif kind == "command":
                self.comparator.command("integration", event.get("command") or {})
            elif kind == "decision":
                self.comparator.decision("integration", str(event.get("window_id")), event.get("decision") or {})
            elif kind == "ownership":
                self._on_ownership(event.get("ownership") or {})
            elif kind == "closed":
                self.session_id = None
                self.sub_id = None
                self.bridge, self.bridge_error = "disconnected", "the integration closed the session"
                self.comparator.stop()
                self._spawn(self._stop_instance(flush=self.mode == "owner"))
                self._connected()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Curtain bridge message %s not usable: %s", kind, err)
        self._changed()

    async def _on_hello(self, hello: dict[str, Any]) -> None:
        try:
            await self._hello(hello)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Curtain bridge session could not start")
            self.bridge, self.bridge_error = "error", f"invalid hello: {err}"
        finally:
            backlog, self._backlog = self._backlog or [], None
            for event in backlog:
                self._dispatch(event)
            self._changed()

    async def _hello(self, hello: dict[str, Any]) -> None:
        self.session_id = hello.get("session_id")
        self.integration = dict(hello.get("ownership") or {})
        self.integration_revision = hello.get("config_revision")
        self.bridge, self.bridge_error = "connected", None
        self.counters["sessions"] += 1
        lease = hello.get("lease_token")
        await self._stop_instance(flush=self.mode == "owner")
        if lease and S.valid_id(lease):
            self._token, self.epoch, self.seq = lease, self.integration.get("epoch"), 0
            self.mode = "owner"
            self._log("resumed", epoch=self.epoch)
            await self._start_owner(config=None, state=None)
            self._apply_renames(hello.get("renames"))
        else:
            if self.mode == "owner":
                self._log("lost", reason="the integration did not keep the App's control")
                self._notice("lost", "Curtain control returned to the integration while the App was away.")
            self._token, self.epoch = None, None
            self.mode = "shadow"
            await self._start_shadow(hello)
        self._changed()

    def _apply_renames(self, renames: Any) -> None:
        mgr = self.instance.manager if self.instance is not None else None
        for item in renames if isinstance(renames, list) and mgr is not None else ():
            if isinstance(item, list) and len(item) == 2 and all(isinstance(e, str) for e in item):
                mgr.rename_entity(item[0], item[1])

    def _on_config(self, event: dict[str, Any]) -> None:
        self.integration_revision = event.get("config_revision")
        inst = self.instance
        if self.mode != "shadow" or inst is None or inst.manager is None:
            return
        from smart_occupancy.curtains.config import CurtainConfig  # noqa: PLC0415

        inst.config_changed(CurtainConfig.from_dict(event.get("config") or {}), event.get("fired"))

    def _on_ownership(self, ownership: dict[str, Any]) -> None:
        self.integration = dict(ownership)
        if self.mode == "owner" and (ownership.get("owner") != S.OWNER_APP or ownership.get("epoch") != self.epoch):
            self._spawn(self._lose("the integration changed the owner"))

    # ------------------------------------------------------------ instances
    def _published(self, area_id: str) -> Any:
        return room_result(self.occupancy, area_id)

    def _backend(self, owner: bool) -> AppBackend:
        if owner:
            return AppBackend(self.ha, execute=self._execute, store_saved=self._store_saved)
        return AppBackend(self.ha, execute=None)

    def _options(self) -> dict[str, Any]:
        rooms = getattr(self.occupancy, "options", None)
        return dict(rooms) if isinstance(rooms, dict) else {}

    async def _start_shadow(self, hello: dict[str, Any]) -> None:
        inst = CurtainInstance(self.ha, role="shadow", options=self._options(), config=hello.get("config") or {},
                               state=hello.get("state"), published=self._published, backend=self._backend(False),
                               storage_dir=None, automations=self.automations, scripts=self.scripts)
        await self._run_instance(inst)
        mgr = inst.manager
        mgr.command_observer = lambda record: self.comparator.command("app", record)
        mgr.decision_observer = lambda window_id, d: self.comparator.decision("app", window_id, d)
        self.comparator.start(utcnow())

    async def _start_owner(self, *, config: dict[str, Any] | None, state: dict[str, Any] | None) -> None:
        store = self.dir / "store" if self.dir else None
        inst = CurtainInstance(self.ha, role="owner", options=self._options(), config=config, state=state,
                               published=self._published, backend=self._backend(True), storage_dir=store,
                               automations=self.automations, scripts=self.scripts)
        await self._run_instance(inst)
        self.comparator.stop()
        inst.manager.state_observer = lambda: self._mark_publish("state")
        inst.manager.config_observer = lambda _new: self._mark_publish("config")
        self._dirty.update({"config", "state"})
        self._schedule_publish(0)
        self._start_heartbeat()

    async def _run_instance(self, inst: CurtainInstance) -> None:
        from homeassistant.helpers.dispatcher import async_dispatcher_connect  # noqa: PLC0415
        from smart_occupancy.curtains.manager import SIGNAL_CURTAINS_UPDATED  # noqa: PLC0415

        self.instance = inst
        await inst.async_start()
        async_dispatcher_connect(inst.hass, SIGNAL_CURTAINS_UPDATED, self._models_changed)

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
            raise CurtainsError(S.REJECT_NOT_OWNER, "The App does not control the curtains")
        try:
            return await self.ha.curtains_command(command, **self._owner_params(), **params)
        except HomeAssistantError as err:
            if err.code in S.LOST_CODES:
                self._spawn(self._lose(f"{command.rsplit('/', 1)[-1]} refused: {err.code}"))
            raise

    async def _execute(self, domain: str, service: str, data: dict[str, Any], context: Context) -> None:
        """The owner manager's only way to a cover (the integration fences and allowlists it)."""
        self.counters["commands"] += 1
        try:
            await self._owner_command("smart_occupancy/curtains/bridge/execute", domain=domain, service=service,
                                      data=data, context_id=context.id)
        except CurtainsError as err:
            raise HostError(str(err)) from err
        except HomeAssistantError as err:
            self.counters["command_errors"] += 1
            self.last_error = {"at": iso(utcnow()), "code": err.code, "detail": _detail(err)}
            if err.code == S.REJECT_NOT_ALLOWED:
                _LOGGER.warning("The integration refused a cover command: %s", _detail(err))
            raise HostError(_detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            self.counters["command_errors"] += 1
            raise HostError("Home Assistant is not connected") from err

    def _store_saved(self, key: str, data: Any) -> None:
        if key == CURTAINS_STORE_KEY:
            self._dirty.add("config")
        elif key == CURTAINS_STATE_STORE_KEY:
            self._dirty.add("state")
        else:
            return
        self._schedule_publish(PUBLISH_DELAY)

    def _mark_publish(self, part: str) -> None:
        if self.mode == "owner":
            self._dirty.add(part)
            self._schedule_publish(PUBLISH_DELAY)

    def _schedule_publish(self, delay: float) -> None:
        if self._publish_handle is None:
            self._publish_handle = asyncio.get_running_loop().call_later(
                delay, lambda: self._spawn(self._publish()))

    def config_doc(self) -> dict[str, Any] | None:
        inst = self.instance
        return inst.manager.config.as_dict() if inst is not None and inst.manager is not None else None

    async def _publish(self) -> None:
        self._publish_handle = None
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.manager is None or not self._dirty:
            return
        dirty, self._dirty = self._dirty, set()
        for part in ("state", "config"):
            if part not in dirty or self.mode != "owner" or self.instance is not inst:
                continue
            if part == "config":
                doc = inst.manager.config.as_dict()
                params: dict[str, Any] = {"config": doc, "config_revision": S.config_revision(doc)}
            else:
                params = {"state": inst.manager.export_state()}
            try:
                await self._owner_command("smart_occupancy/curtains/bridge/publish", **params)
                self.counters["publishes"] += 1
            except (CurtainsError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                self._dirty.add(part)
                self.last_error = {"at": iso(utcnow()), "code": getattr(err, "code", type(err).__name__),
                                   "detail": str(err)}
                if self.mode == "owner":
                    self._schedule_publish(10)

    def _start_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while self.mode == "owner" and not self._stopping:
            await asyncio.sleep(S.HEARTBEAT_SECONDS)
            if self.mode != "owner":
                return
            try:
                reply = await self._owner_command("smart_occupancy/curtains/bridge/heartbeat")
                self.lease_until = (reply or {}).get("lease_until")
            except (CurtainsError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug("curtains heartbeat failed: %s", err)

    async def _lose(self, why: str) -> None:
        if self.mode != "owner" or self._switching:
            return
        _LOGGER.warning("The App no longer controls the curtains (%s); back to shadow mode", why)
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

    def discard_shadow(self, why: str) -> None:
        """The shadow mirrored a command the integration then refused: its copy diverged. A fresh bridge session
        starts a new shadow from the integration's exact copy (its pending comparisons are dropped first)."""
        if self.mode != "shadow" or not self.ha.connected:
            return
        _LOGGER.info("curtains shadow discarded (%s): starting from a fresh copy", why)
        self.comparator.stop()
        self._spawn(self._resubscribe())

    # -------------------------------------------------------- handover, return
    def moving_windows(self) -> list[str]:
        inst = self.instance
        mgr = inst.manager if inst else None
        if mgr is None:
            return []
        return sorted(w for w in mgr.config.windows if mgr.running(w) is not None)

    def readiness(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        report = self.comparator.report(now)
        checks: list[dict[str, Any]] = []

        def check(key: str, ok: bool, title: str, detail: str, warning: bool = False) -> None:
            checks.append({"key": key, "status": "ok" if ok else "warning" if warning else "error", "title": title,
                           "detail": detail})

        inst = self.instance
        integ = self.integration
        mgr = inst.manager if inst else None
        app_revision = S.config_revision(mgr.config.as_dict()) if mgr else None
        moving = self.moving_windows() or list(integ.get("moving") or [])
        check("bridge", self.bridge == "connected", "Curtain bridge connected", self.bridge_error or self.bridge)
        check("fingerprint", bool(self.fingerprint) and self.fingerprint == integ.get("fingerprint"),
              "Same curtain code", "The App and the integration run identical curtain modules"
              if self.fingerprint == integ.get("fingerprint") else "Update the App and the integration together")
        check("core", bool(self.core_fingerprint) and self.core_fingerprint == integ.get("core_fingerprint"),
              "Same occupancy engine", "Identical occupancy engine code on both sides")
        check("config", app_revision is not None and app_revision == self.integration_revision,
              "Configuration in sync", f"App {app_revision or '-'}, integration {self.integration_revision or '-'}")
        check("owner", integ.get("owner") == S.OWNER_INTEGRATION and integ.get("state") == S.OWNER_INTEGRATION,
              "The integration controls the curtains", f"owner {integ.get('owner') or '-'}")
        check("duration", report["seconds"] >= self.min_shadow_seconds, "Compared long enough",
              f"{report['seconds'] // 60} of {self.min_shadow_seconds // 60} min")
        check("decisions", report["decisions"]["matched"] >= self.min_decisions, "Enough matching decisions",
              f"{report['decisions']['matched']} of {self.min_decisions} window decisions matched")
        check("mismatches", report["commands"]["mismatches"] == 0, "No cover command mismatch",
              f"{report['commands']['mismatches']} since the comparison started")
        check("moving", not moving, "No cover is moving",
              "Every window is at rest" if not moving else f"Still moving: {', '.join(moving)}")
        check("differences", report["decisions"]["differences"] == 0, "Decisions agree",
              f"{report['decisions']['differences']} decision difference(s); review them below", warning=True)
        ready = all(c["status"] == "ok" for c in checks if c["status"] != "warning") and self.mode == "shadow"
        return {"ready": ready, "checks": checks, "summary": {
            "seconds": report["seconds"], "matched": report["decisions"]["matched"],
            "command_matches": report["commands"]["matched"], "mismatches": report["commands"]["mismatches"],
            "decision_differences": report["decisions"]["differences"]},
            "thresholds": {"min_shadow_seconds": self.min_shadow_seconds, "min_decisions": self.min_decisions}}

    async def handover(self, user_id: str | None) -> dict[str, Any]:
        if self.mode == "owner":
            raise CurtainsError("already_owner", "The App already controls the curtains")
        if self._switching:
            raise CurtainsError("busy", "A change of control is in progress")
        ready = self.readiness()
        if not ready["ready"]:
            failed = [c["title"] for c in ready["checks"] if c["status"] == "error"]
            raise CurtainsError("not_ready", "Not ready for a handover: " + ", ".join(failed))
        self._switching = True
        try:
            try:
                grant = await self.ha.curtains_command(
                    "smart_occupancy/curtains/bridge/claim", session_id=self.session_id,
                    expected_epoch=self.integration.get("epoch"), config_revision=self.integration_revision,
                    readiness={k: ready[k] for k in ("ready", "checks", "summary")}, requested_by=user_id)
            except HomeAssistantError as err:
                raise CurtainsError(err.code, _detail(err)) from err
            except (ConnectionError, TimeoutError) as err:
                raise CurtainsError("not_connected", "Home Assistant is not connected", 503) from err
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
            await self._start_owner(config=grant["config"], state=grant["state"])
            check = self.verify_copy(grant["config"])
        finally:
            self._switching = False
        _LOGGER.warning("Curtain handover: the App now controls the curtains (epoch %s)", self.epoch)
        self._changed()
        return {"epoch": self.epoch, "copy": check}

    def verify_copy(self, source: dict[str, Any]) -> dict[str, Any]:
        doc = self.config_doc() or {}
        differences = sorted(k for k in set(source) | set(doc) if source.get(k) != doc.get(k))
        return {"source_revision": S.config_revision(source), "destination_revision": S.config_revision(doc),
                "identical": not differences, "differences": differences[:50],
                "windows": len(doc.get("windows") or []), "keys": len(doc)}

    def _backup(self, data: dict[str, Any], reason: str) -> None:
        if self.dir is None:
            return
        folder = self.dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        payload = {"schema": EXPORT_SCHEMA, "reason": reason, "at": iso(utcnow()), "config": data.get("config"),
                   "config_revision": data.get("config_revision"), "state": data.get("state")}
        path = folder / f"curtains-{stamp}-{reason}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)
        for old in sorted(folder.glob("curtains-*.json"))[:-BACKUPS]:
            old.unlink(missing_ok=True)

    def backups(self) -> list[dict[str, Any]]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not folder.is_dir():
            return []
        return [{"id": p.stem, "size": p.stat().st_size} for p in sorted(folder.glob("curtains-*.json"), reverse=True)]

    def export(self) -> dict[str, Any]:
        """The current curtain configuration and state (the rollback / backup export)."""
        inst = self.instance
        mgr = inst.manager if inst else None
        doc = mgr.config.as_dict() if mgr else None
        return {"schema": EXPORT_SCHEMA, "at": iso(utcnow()), "mode": self.mode, "config": doc,
                "config_revision": S.config_revision(doc) if doc is not None else None,
                "state": mgr.export_state() if mgr else None}

    def import_review(self, doc: Any) -> dict[str, Any]:
        """What restoring an exported configuration would change (nothing is changed)."""
        from smart_occupancy.curtains.config import CurtainConfig  # noqa: PLC0415

        if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA or not isinstance(doc.get("config"), dict):
            raise CurtainsError("invalid_export", "This is not a Smart Lighting curtain export", 400)
        try:
            incoming = CurtainConfig.from_dict(doc["config"])
        except (TypeError, ValueError, KeyError, AttributeError) as err:
            raise CurtainsError("invalid_export", f"The exported configuration is not valid: {err}", 400) from err
        current = self.config_doc() or {}
        new = incoming.as_dict()
        unknown = sorted(e for e in S.covers_of(new) if e not in self.ha.states)
        changes = sorted(k for k in set(current) | set(new) if current.get(k) != new.get(k))
        dropped = sorted(set(S.covers_of(doc["config"])) - set(S.covers_of(new)))
        return {"writable": self.mode == "owner", "revision": S.config_revision(current),
                "incoming_revision": S.config_revision(new), "changes": changes, "windows": len(new.get("windows") or []),
                "missing_entities": unknown, "dropped_entities": dropped,
                "exported_at": doc.get("at"), "same": not changes}

    async def import_apply(self, doc: Any, expected_revision: Any) -> dict[str, Any]:
        """Restore an exported configuration into the App's own store (only while the App controls the curtains)."""
        from smart_occupancy.curtains.config import CurtainConfig  # noqa: PLC0415

        review = self.import_review(doc)
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.manager is None:
            raise CurtainsError("not_owner", "The App does not control the curtains: restore the configuration with the "
                                             "integration's tools, or hand the curtains over first")
        if expected_revision != review["revision"]:
            raise CurtainsError("stale_revision", "The curtain configuration changed meanwhile; review again")
        self._backup(self.export(), "import")
        inst.manager.async_set(CurtainConfig.from_dict(doc["config"]))
        return {"applied": True, "revision": S.config_revision(self.config_doc() or {})}

    async def release(self, user_id: str | None) -> dict[str, Any]:
        if self.mode != "owner" or self.instance is None:
            return await self.return_control(user_id)
        if self._switching:
            raise CurtainsError("busy", "A change of control is in progress")
        self._switching = True
        lost = False
        try:
            inst = self.instance
            mgr = inst.manager
            mgr.fenced = True
            if not await mgr.async_drain(S.MOVING_WAIT_SECONDS):
                mgr.fenced = False
                raise CurtainsError("moving", "A cover is still being moved; wait until it has reached its position")
            final = self.export()
            self._backup(final, "release")
            params = {"config": final["config"], "config_revision": final["config_revision"],
                      "state": final["state"], "detail": "returned from the App"}
            try:
                reply = await self._owner_command("smart_occupancy/curtains/bridge/release", **params)
            except (CurtainsError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                mgr.fenced = False
                lost = getattr(err, "code", None) in S.LOST_CODES
                raise CurtainsError(getattr(err, "code", None) or "not_connected", _detail(err)) from err
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
            if lost:
                self._spawn(self._lose("release refused"))
        await self._resubscribe()
        self._changed()
        return {"ownership": self.integration}

    async def return_control(self, user_id: str | None) -> dict[str, Any]:
        if self.mode == "owner" and self.instance is not None and not self._switching:
            try:
                return await self.release(user_id)
            except CurtainsError as err:
                if err.code == "moving":
                    raise
                _LOGGER.warning("Release refused (%s); returning control without the final state", err)
        mgr = self.instance.manager if self.mode == "owner" and self.instance is not None else None
        if mgr is not None:
            mgr.fenced = True
            await mgr.async_drain(S.MOVING_WAIT_SECONDS)
        try:
            reply = await self.ha.curtains_command("smart_occupancy/curtains/bridge/return",
                                                   reason="returned from the App")
        except HomeAssistantError as err:
            if mgr is not None and self.mode == "owner" and err.code not in S.LOST_CODES:
                mgr.fenced = False
            raise CurtainsError(err.code, _detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            if mgr is not None and self.mode == "owner":
                mgr.fenced = False
            raise CurtainsError("not_connected", "Home Assistant is not connected", 503) from err
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
        mgr = inst.manager if inst else None
        return {
            "mode": self.mode, "bridge": self.bridge, "bridge_error": self.bridge_error,
            "instance_id": self.instance_id[:8], "session_id": (self.session_id or "")[:8] or None,
            "app_version": self.app_version, "runtime_version": RUNTIME_VERSION,
            "fingerprint": self.fingerprint, "core_fingerprint": self.core_fingerprint,
            "integration": {k: integ.get(k) for k in (
                "owner", "state", "epoch", "integration_version", "fingerprint", "core_fingerprint", "lease_until",
                "changed_at", "changed_by", "config_revision", "manager", "moving", "windows", "executed",
                "execute_errors", "rejections", "history")},
            "epoch": self.epoch, "lease_until": self.lease_until,
            "config_revision": S.config_revision(mgr.config.as_dict()) if mgr else None,
            "integration_revision": self.integration_revision,
            "running": bool(inst and inst.started), "moving": self.moving_windows(),
            "counters": dict(self.counters), "last_error": self.last_error,
            "notices": self.notices, "ownership_log": list(self.ownership_log),
            "comparison": self.comparator.report(now), "readiness": self.readiness(now),
        }

    @property
    def manager(self) -> Any:
        inst = self.instance
        return inst.manager if inst is not None else None
