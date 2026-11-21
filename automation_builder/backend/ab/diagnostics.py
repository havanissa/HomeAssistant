"""Rule-level execution diagnostics, read from Home Assistant's own automation traces.

Automation Builder runs nothing itself. To say what a *rule* did, this module
reads the traces Home Assistant keeps of the managed automations and interprets
them with the knowledge of how the compiler laid the automation out (one ``if``
block per rule, see ``compiler.py``):

* **triggered**: the run was started by one of the rule's triggers (the first
  condition of the rule's block, the trigger check, was true);
* **blocked**: it was triggered, but one of the rule's conditions was false -
  which one is named;
* **executed**: the rule's actions started;
* **outcome**: finished, still running, or failed with Home Assistant's error.

These are different things, and they stay different here: a triggered rule did
not necessarily execute, and an executed action is a service call that Home
Assistant accepted, nothing more.

**Action verification** goes one step further where that is reliable. For a
service call whose result can be read from the entity (``catalog.effect``:
on/off, open/closed, a selected option; plus brightness, colour temperature,
position, target temperature and a few more attributes), the entity's
*reported* state is compared with the expectation a few seconds after the call:

* ``confirmed``   the reported state matches;
* ``mismatch``    it does not, and nothing else touched the entity since;
* ``changed_since``  something else changed the entity after the call (another
  automation, a person): not judged;
* ``unavailable`` / ``not_verifiable``: nothing can be said.

A reported state is what the integration says, not proof of what a physical
device did. A mismatch is shown; nothing is corrected from here - corrections
only ever come from rules the user configured (the occupancy rechecks).

Home Assistant keeps only the last few traces and forgets them at a restart, so
the observations are stored (``/data/diag/rules.json``) as they are seen.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Any

from . import catalog, describe
from .store import _atomic_write

_LOGGER = logging.getLogger(__name__)

SETTLE_SECONDS = 5.0        # how long after a call the reported state is looked at
VERIFY_LATEST = 180.0       # a run seen later than this is not verified (the state says nothing about it anymore)
REFRESH_DELAY = 2.0         # after an automation was triggered, its trace is read this much later
RUNNING_RECHECK = 20.0      # a run that still waits is looked at again
SEEN = 60
NOT_READY = ("unavailable", "unknown")

RECHECK_CHECKS = ("the occupancy sensor was not in the rule's state anymore",
                  "the occupancy sensor reported stale data (fresh: false)",
                  "the occupancy change came too soon after a start of Home Assistant or a deployment "
                  "(start-up grace)")
RECHECK_LAST = "the device was already as the rule leaves it, or it was changed after the occupancy change"


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def blocks_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Which action of a deployed automation belongs to which rule (stored at deployment; derived for older records)."""
    if isinstance(record.get("blocks"), list):
        return record["blocks"]
    by_name = {r["name"]: r["id"] for r in (record.get("device") or {}).get("rules", [])}
    out = []
    for index, action in enumerate((record.get("config") or {}).get("actions", [])):
        kind, _, name = str(action.get("alias", "")).partition(": ")
        if kind in ("Rule", "Recheck") and name in by_name:
            out.append({"rule_id": by_name[name], "kind": kind.lower(), "index": index})
    return out


def _result(trace: dict[str, Any], path: str) -> Any:
    steps = trace.get(path) or []
    return (steps[-1].get("result") or {}) if steps else None


def _blocked_by(rule: dict[str, Any] | None, kind: str, position: int, count: int, names: Any) -> str:
    """The text of the condition at ``position`` (0 = the first after the trigger check) of a rule's block."""
    if kind == "recheck":
        if position < len(RECHECK_CHECKS):
            return RECHECK_CHECKS[position]
        if position == count - 1:
            return RECHECK_LAST
        position -= len(RECHECK_CHECKS)
        if rule and rule["recheck"]["override_entity"]:
            if position == 0:
                return f"the manual override signal {names(rule['recheck']['override_entity'])} was on"
            position -= 1
    if rule is None:
        return f"condition {position + 1}"
    group = rule["conditions"]
    if group["type"] == "and" and position < len(group["items"]):
        return describe.condition(group["items"][position], names, triggers=rule["triggers"])
    return describe.condition(group, names, top=True, triggers=rule["triggers"])


def interpret(trace: dict[str, Any], blocks: list[dict[str, Any]], rules: dict[str, dict[str, Any]],
              names: Any = None) -> list[dict[str, Any]]:
    """One entry per rule block that this run concerned: what happened to the rule in this run."""
    names = names or (lambda entity_id: entity_id)
    steps = trace.get("trace") or {}
    started = (trace.get("timestamp") or {}).get("start")
    out = []
    for block in blocks:
        base = block.get("path") or f"action/{block['index']}"
        gate = _result(steps, f"{base}/if/condition/0")
        if not gate or gate.get("result") is not True:
            continue  # the run was started by a trigger of another rule
        rule = rules.get(block["rule_id"])
        depth = base.count("/") + 3
        conditions = sorted(int(p.rsplit("/", 1)[1]) for p in steps
                            if p.startswith(f"{base}/if/condition/") and p.count("/") == depth)
        try:  # how many conditions the block has (a trace stops at the first that is false)
            total = len(trace["config"]["actions"][block["index"]]["if"]) if "path" not in block else max(conditions) + 1
        except (KeyError, IndexError, TypeError, ValueError):
            total = (max(conditions) + 1) if conditions else 1
        blocked = None
        for position in conditions[1:]:
            result = _result(steps, f"{base}/if/condition/{position}")
            if result and result.get("result") is False:
                blocked = _blocked_by(rule, block["kind"], position - 1, total - 1, names)
                break
        prefix = f"{base}/then/"
        executed = any(p.startswith(prefix) for p in steps)
        calls, error = [], None
        for path in sorted((p for p in steps if p.startswith(prefix)), key=lambda p: [int(x) if x.isdigit() else x for x in p.split("/")]):
            for step in steps[path]:
                error = error or step.get("error")
                params = (step.get("result") or {}).get("params")
                if isinstance(params, dict) and params.get("domain") and params.get("service"):
                    data = {k: v for k, v in (params.get("service_data") or {}).items() if k != "entity_id"}
                    entities = (params.get("target") or {}).get("entity_id") or []
                    calls.append({"at": step.get("timestamp"), "action": f"{params['domain']}.{params['service']}",
                                  "entities": [entities] if isinstance(entities, str) else list(entities), "data": data})
        last = str(trace.get("last_step") or "")
        mine = last == base or last.startswith(base + "/")
        if not executed:
            outcome = "blocked" if blocked else "not_run"
        elif error or (mine and trace.get("error")):
            outcome, error = "failed", error or trace.get("error")
        elif trace.get("state") == "running":
            outcome = "running"
        elif mine and trace.get("script_execution") not in (None, "finished"):
            outcome = str(trace.get("script_execution"))
        else:
            outcome = "finished"
        out.append({"rule_id": block["rule_id"], "kind": block["kind"], "run_id": trace.get("run_id"), "at": started,
                    "triggered": True, "executed": executed, "blocked_by": blocked, "outcome": outcome,
                    "error": str(error) if error else None, "calls": calls,
                    "context_id": (trace.get("context") or {}).get("id"),
                    "finished_at": (trace.get("timestamp") or {}).get("finish")})
    return out


# ------------------------------------------------------------ verification
def _attribute_expectations(action: str, data: dict[str, Any], attributes: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """``{attribute: (expected value, tolerance)}`` that this call should leave on the entity."""
    domain, _, service = action.partition(".")
    out: dict[str, tuple[float, float]] = {}

    def number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    if action == "light.turn_on":
        if (pct := number("brightness_pct")) is not None:
            out["brightness"] = (round(pct * 255 / 100), 4)
        elif (level := number("brightness")) is not None:
            out["brightness"] = (level, 3)
        if (kelvin := number("color_temp_kelvin")) is not None:
            low, high = attributes.get("min_color_temp_kelvin"), attributes.get("max_color_temp_kelvin")
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                kelvin = min(max(kelvin, low), high)  # a lamp answers with the nearest it can do
            out["color_temp_kelvin"] = (kelvin, max(60.0, kelvin * 0.03))
    elif action == "cover.set_cover_position" and (position := number("position")) is not None:
        out["current_position"] = (position, 2)
    elif domain in ("climate", "water_heater") and service == "set_temperature" and (t := number("temperature")) is not None:
        out["temperature"] = (t, 0.26)
    elif domain == "fan" and service in ("turn_on", "set_percentage") and (pct := number("percentage")) is not None:
        step = attributes.get("percentage_step")
        out["percentage"] = (pct, float(step) if isinstance(step, (int, float)) and step > 0 else 2)
    elif domain == "humidifier" and service == "set_humidity" and (h := number("humidity")) is not None:
        out["humidity"] = (h, 1)
    elif domain == "media_player" and service == "volume_set" and (v := number("volume_level")) is not None:
        out["volume_level"] = (v, 0.02)
    return out


def expectations(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per entity, what the last call of a run to it should leave behind (``verifiable`` False when unknown)."""
    out: dict[str, dict[str, Any]] = {}
    for call in calls:
        effect = catalog.effect(call["action"], call["data"])
        for entity_id in call["entities"]:
            entry: dict[str, Any] = {"action": call["action"], "data": call["data"], "at": call["at"],
                                     "states": None, "value": None, "verifiable": True, "why": None}
            domain = call["action"].split(".", 1)[0]
            if effect["kind"] == "state":
                entry["states"] = effect["states"]
            elif effect["kind"] == "non_idempotent":
                entry.update(verifiable=False, why="the action does something different every time (toggle, press …)")
            elif effect["kind"] == "unknown":
                entry.update(verifiable=False, why="Automation Builder does not know what this action leaves behind")
            elif domain in ("number", "input_number") and call["action"].endswith(".set_value") \
                    and isinstance(call["data"].get("value"), (int, float)):
                entry["value"] = float(call["data"]["value"])
            out[entity_id] = entry
    return out


def verify(entity_id: str, expected: dict[str, Any], state: dict[str, Any] | None, context_id: str | None) -> dict[str, Any]:
    """Compare what one entity reports with what the rule's last call to it should have left."""
    base = {"entity_id": entity_id, "action": expected["action"], "expected": None, "reported": None, "detail": None}
    if not expected["verifiable"]:
        return {**base, "verdict": "not_verifiable", "detail": expected["why"]}
    if state is None or state.get("state") in NOT_READY:
        return {**base, "verdict": "unavailable", "reported": (state or {}).get("state"),
                "detail": "the entity reports no state"}
    attributes = state.get("attributes") or {}
    wanted_attributes = _attribute_expectations(expected["action"], expected["data"], attributes)
    if expected["states"] is None and expected["value"] is None and not wanted_attributes:
        return {**base, "verdict": "not_verifiable",
                "detail": "the result of this action cannot be read from the entity's state"}
    wrong, wanted, reported = [], [], []
    if expected["states"] is not None:
        wanted.append("“" + "” or “".join(expected["states"]) + "”")
        reported.append(f"“{state.get('state')}”")
        if state.get("state") not in expected["states"]:
            wrong.append("state")
    if expected["value"] is not None:
        wanted.append(f"{expected['value']:g}")
        reported.append(str(state.get("state")))
        try:
            if abs(float(state.get("state")) - expected["value"]) > 1e-6:  # type: ignore[arg-type]
                wrong.append("value")
        except (TypeError, ValueError):
            wrong.append("value")
    on = expected["states"] is None or state.get("state") in (expected["states"] or [])
    for name, (value, tolerance) in wanted_attributes.items():
        actual = attributes.get(name)
        wanted.append(f"{name.replace('_', ' ')} {value:g}")
        reported.append(f"{name.replace('_', ' ')} {actual if actual is not None else 'not reported'}")
        if not on:
            continue
        if not isinstance(actual, (int, float)) or abs(float(actual) - value) > tolerance:
            wrong.append(name)
    out = {**base, "expected": ", ".join(wanted), "reported": ", ".join(reported)}
    if not wrong:
        return {**out, "verdict": "confirmed"}
    changed_by_other = bool(context_id) and (state.get("context") or {}).get("id") not in (None, context_id) \
        and (_parse(state.get("last_updated")) or datetime.min.replace(tzinfo=UTC)) > (_parse(expected["at"]) or datetime.max.replace(tzinfo=UTC))
    if changed_by_other:
        return {**out, "verdict": "changed_since",
                "detail": "something else changed the entity after the rule's action, so the action is not judged"}
    return {**out, "verdict": "mismatch",
            "detail": "Home Assistant accepted the action, but the entity does not report the expected "
                      + " and ".join(w.replace("_", " ") for w in wrong)}


def settle_seconds(calls: list[dict[str, Any]]) -> float:
    transitions = [c["data"].get("transition") for c in calls]
    longest = max((float(t) for t in transitions if isinstance(t, (int, float)) and not isinstance(t, bool)), default=0.0)
    return SETTLE_SECONDS + min(longest, 300.0)


# ------------------------------------------------------------------- store
class RuleStats:
    """What was observed per rule, kept across restarts of the App and of Home Assistant."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "diag" / "rules.json"
        self.rules: dict[str, dict[str, Any]] = {}
        self.seen: dict[str, dict[str, str]] = {}  # automation id -> {run:rule:kind -> outcome}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(document.get("rules"), dict) and isinstance(document.get("seen"), dict) \
                    and all(isinstance(v, dict) for v in document["seen"].values()):
                self.rules, self.seen = document["rules"], document["seen"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as err:
            _LOGGER.warning("The rule diagnostics could not be read (%s); starting empty", err)

    def save(self) -> None:
        try:
            _atomic_write(self.path, {"schema": 1, "rules": self.rules, "seen": self.seen})
        except OSError as err:
            _LOGGER.warning("The rule diagnostics could not be saved: %s", err)

    def of(self, rule_id: str) -> dict[str, Any]:
        return self.rules.setdefault(rule_id, {"runs": 0, "executions": 0, "blocks": 0})

    def forget(self, keep: set[str]) -> None:
        for rule_id in [r for r in self.rules if r not in keep]:
            del self.rules[rule_id]

    def known(self, automation_id: str, run_id: str) -> str | None:
        """``None`` for a run not seen yet, ``"running"`` for one that was still running, else ``"done"``."""
        outcomes = [o for key, o in self.seen.get(automation_id, {}).items() if key.startswith(f"{run_id}:")]
        if not outcomes:
            return None
        return "running" if "running" in outcomes else "done"

    def mark(self, automation_id: str, key: str, outcome: str) -> None:
        seen = self.seen.setdefault(automation_id, {})
        seen[key] = outcome
        for old in list(seen)[:-SEEN]:
            del seen[old]

    def apply(self, automation_id: str, entry: dict[str, Any]) -> bool:
        """Record one interpreted run of a rule. Returns whether anything was new."""
        key = f"{entry['run_id']}:{entry['rule_id']}:{entry['kind']}"
        before = self.seen.get(automation_id, {}).get(key)
        if before is not None and before != "running":
            return False
        fresh = before is None
        self.mark(automation_id, key, entry["outcome"])
        stats = self.of(entry["rule_id"])
        ref = {"at": entry["at"], "run_id": entry["run_id"], "automation_id": automation_id}

        def newest(name: str) -> bool:
            return (entry["at"] or "") >= ((stats.get(name) or {}).get("at") or "")

        if entry["kind"] == "recheck":
            if newest("last_recheck"):
                stats["last_recheck"] = {**ref, "executed": entry["executed"], "skipped_because": entry["blocked_by"]}
            if not entry["executed"]:
                return True
            if fresh:
                stats["rechecks"] = stats.get("rechecks", 0) + 1
        elif fresh:
            stats["runs"] = stats.get("runs", 0) + 1
            if newest("last_triggered"):
                stats["last_triggered"] = ref
        if entry["executed"]:
            if fresh:
                stats["executions"] = stats.get("executions", 0) + 1
            if newest("last_executed"):
                stats["last_executed"] = {**ref, "recheck": entry["kind"] == "recheck"}
                stats["last_outcome"] = {**ref, "outcome": entry["outcome"], "error": entry["error"],
                                         "actions": len(entry["calls"])}
                stats["verification"] = {**ref, "state": "waiting" if entry["outcome"] == "running" else "pending",
                                         "checks": []}
        elif entry["blocked_by"]:
            if fresh:
                stats["blocks"] = stats.get("blocks", 0) + 1
            if newest("last_blocked"):
                stats["last_blocked"] = {**ref, "by": entry["blocked_by"]}
        return True


class Observer:
    """Reads the traces of the managed automations when they were triggered, and verifies what can be verified."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.stats = RuleStats(core.settings.data_dir)
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._last_triggered: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.settle = SETTLE_SECONDS

    # ------------------------------------------------------------ events
    def on_state(self, entity_id: str, state: dict[str, Any] | None) -> None:
        """A state changed in Home Assistant: was one of this App's automations triggered?"""
        if not entity_id.startswith("automation.") or state is None:
            return
        attributes = state.get("attributes") or {}
        automation_id = attributes.get("id")
        if automation_id not in self.core.deploys.records:
            return
        triggered = attributes.get("last_triggered")
        if triggered and triggered != self._last_triggered.get(automation_id):
            self._last_triggered[automation_id] = triggered
            self.schedule(automation_id, REFRESH_DELAY)

    def schedule(self, automation_id: str, delay: float) -> None:
        loop = asyncio.get_running_loop()
        if (timer := self._timers.pop(automation_id, None)) is not None:
            timer.cancel()
        self._timers[automation_id] = loop.call_later(delay, self._spawn, automation_id)

    def _spawn(self, automation_id: str) -> None:
        self._timers.pop(automation_id, None)
        task = asyncio.get_running_loop().create_task(self.refresh(automation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in list(self._tasks):
            task.cancel()

    # ----------------------------------------------------------- reading
    async def refresh(self, automation_id: str) -> None:
        """Read the traces of one managed automation and record what is new."""
        ha, record = self.core.ha, self.core.deploys.records.get(automation_id)
        if ha is None or not ha.connected or record is None:
            return
        async with self._locks.setdefault(automation_id, asyncio.Lock()):
            try:
                listed = await ha.command("trace/list", domain="automation", item_id=automation_id) or []
            except Exception as err:  # noqa: BLE001 - diagnostics must never break anything
                _LOGGER.debug("Could not list the traces of %s: %s", automation_id, err)
                return
            blocks = blocks_of(record)
            rules = {r["id"]: r for r in (record.get("device") or {}).get("rules", [])}
            names = self.core.index().name
            running = changed = False
            for item in sorted((t for t in listed if isinstance(t, dict)),
                               key=lambda t: (t.get("timestamp") or {}).get("start") or ""):
                run_id = str(item.get("run_id"))
                if self.stats.known(automation_id, run_id) == "done":
                    continue
                try:
                    trace = await ha.command("trace/get", domain="automation", item_id=automation_id, run_id=run_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not read trace %s of %s: %s", run_id, automation_id, err)
                    continue
                if not isinstance(trace, dict):
                    continue
                entries = interpret(trace, blocks, rules, names)
                if not entries:  # a run that concerned no rule (a periodic check that ended at once)
                    self.stats.mark(automation_id, f"{run_id}:-:-", "running" if trace.get("state") == "running" else "none")
                for entry in entries:
                    if self.stats.apply(automation_id, entry):
                        changed = True
                        if entry["executed"] and entry["outcome"] != "running":
                            self._plan_verification(entry)
                    running = running or entry["outcome"] == "running"
            if running:
                self.schedule(automation_id, RUNNING_RECHECK)
            if changed:
                self.stats.save()

    def _plan_verification(self, entry: dict[str, Any]) -> None:
        stats = self.stats.of(entry["rule_id"])
        if (stats.get("verification") or {}).get("run_id") != entry["run_id"]:
            return  # a newer run of the rule took its place
        if not entry["calls"]:
            stats["verification"] = {**stats["verification"], "state": "nothing", "checks": []}
            return
        finished = _parse(entry["finished_at"]) or _parse(entry["at"]) or datetime.now(UTC)
        age = (datetime.now(UTC) - finished).total_seconds()
        if age > VERIFY_LATEST:
            stats["verification"] = {**stats["verification"], "state": "too_late", "checks": []}
            return
        wait = max(0.0, self.settle - SETTLE_SECONDS + settle_seconds(entry["calls"]) - age)
        asyncio.get_running_loop().call_later(wait, self._verify, entry)

    def _verify(self, entry: dict[str, Any]) -> None:
        stats = self.stats.of(entry["rule_id"])
        if (stats.get("verification") or {}).get("run_id") != entry["run_id"] or self.core.ha is None:
            return
        states = self.core.ha.states
        checks = [verify(entity_id, expected, states.get(entity_id), entry["context_id"])
                  for entity_id, expected in sorted(expectations(entry["calls"]).items())]
        verdicts = {c["verdict"] for c in checks}
        state = ("mismatch" if "mismatch" in verdicts else "confirmed" if "confirmed" in verdicts
                 else "changed_since" if "changed_since" in verdicts else "unavailable" if "unavailable" in verdicts
                 else "not_verifiable")
        stats["verification"] = {**stats["verification"], "state": state, "checks": checks,
                                 "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if state == "mismatch":
            _LOGGER.info("Rule %s: %s", entry["rule_id"], "; ".join(
                f"{c['entity_id']} expected {c['expected']}, reports {c['reported']}" for c in checks if c["verdict"] == "mismatch"))
        self.stats.save()

    # ------------------------------------------------------------- views
    def view(self, rule_id: str) -> dict[str, Any]:
        stats = self.stats.rules.get(rule_id) or {}
        return {key: stats.get(key) for key in ("runs", "executions", "blocks", "rechecks", "last_triggered",
                                                "last_executed", "last_blocked", "last_outcome", "last_recheck",
                                                "verification")}


def recent(iso: str | None, seconds: float) -> bool:
    parsed = _parse(iso)
    return parsed is not None and datetime.now(UTC) - parsed < timedelta(seconds=seconds)
