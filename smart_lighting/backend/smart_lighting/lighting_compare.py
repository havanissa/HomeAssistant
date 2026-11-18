"""Comparison of the App's shadow lighting controller with the integration's (Phase A4).

Both controllers run the same code on the same events; the integration's
commands and decisions arrive over the lighting bridge, the shadow's are
recorded where they are made (``LightingController.command_observer`` and
``DecisionLog.observer``), never sent.

* **Commands** match when the same service, the same targets and the same data
  appear on both sides within :data:`WINDOW_SECONDS`. A command left without a
  partner after the window is a *mismatch* and is kept with both sides' recent
  commands of the same lights.
* **Decisions** (what each controller decided and why: trigger, kind, target,
  status - which covers daylight and ambient decisions, vacancy, overrides,
  switches, the supervisor and priority inputs) match on subject, kind, trigger,
  status and target. Decision differences are reported; only command
  mismatches block a handover, because only commands reach a device.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import UTC, datetime
import json
from typing import Any

WINDOW_SECONDS = 3.0
HISTORY = 100


def _now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _entities(data: dict[str, Any]) -> tuple[str, ...]:
    raw = data.get("entity_id")
    ids = [raw] if isinstance(raw, str) else list(raw or [])
    return tuple(sorted(str(e) for e in ids))


def command_key(record: dict[str, Any]) -> str:
    data = dict(record.get("data") or {})
    entities = _entities(data)
    data.pop("entity_id", None)
    return json.dumps([record.get("domain"), record.get("service"), entities, data], sort_keys=True, default=str)


def decision_key(subject: str, decision: dict[str, Any]) -> str:
    return json.dumps([subject, decision.get("kind"), decision.get("trigger"), decision.get("status"),
                       decision.get("target")], default=str)


class _Side:
    def __init__(self) -> None:
        self.pending: list[tuple[datetime, str, dict[str, Any]]] = []
        self.recent: deque[dict[str, Any]] = deque(maxlen=40)


class LightingComparator:
    def __init__(self) -> None:
        self.running = False
        self.started_at: datetime | None = None
        self.matched = 0
        self.mismatches = 0
        self.by_purpose: Counter[str] = Counter()
        self.mismatch_by_purpose: Counter[str] = Counter()
        self.decisions_matched = 0
        self.decision_differences = 0
        self.discrepancies: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._commands = {"app": _Side(), "integration": _Side()}
        self._decisions = {"app": _Side(), "integration": _Side()}
        self.last_match_at: datetime | None = None
        self.last_mismatch_at: datetime | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self, now: datetime | None = None) -> None:
        """A new comparison (a new bridge session starts both controllers from the same copy)."""
        self.running = True
        self.started_at = now or _now()
        for sides in (self._commands, self._decisions):
            for side in sides.values():
                side.pending.clear()

    def stop(self) -> None:
        self.running = False
        for sides in (self._commands, self._decisions):
            for side in sides.values():
                side.pending.clear()

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    # ------------------------------------------------------------ commands
    def command(self, who: str, record: dict[str, Any], now: datetime | None = None) -> None:
        if not self.running:
            return
        now = now or _now()
        self.sweep(now)
        key = command_key(record)
        other = self._commands["integration" if who == "app" else "app"]
        mine = self._commands[who]
        mine.recent.append({"at": iso(now), **_brief(record)})
        for i, (_, other_key, _) in enumerate(other.pending):
            if other_key == key:
                del other.pending[i]
                self.matched += 1
                self.by_purpose[str(record.get("purpose") or "lighting")] += 1
                self.last_match_at = now
                return
        mine.pending.append((now, key, record))

    def decision(self, who: str, subject: str, decision: dict[str, Any], now: datetime | None = None) -> None:
        if not self.running or subject.startswith("forecast"):
            return
        now = now or _now()
        self.sweep(now)
        key = decision_key(subject, decision)
        other = self._decisions["integration" if who == "app" else "app"]
        for i, (_, other_key, _) in enumerate(other.pending):
            if other_key == key:
                del other.pending[i]
                self.decisions_matched += 1
                return
        self._decisions[who].pending.append((now, key, {"subject": subject, **_decision_brief(decision)}))

    def sweep(self, now: datetime | None = None) -> None:
        now = now or _now()
        for who, side in self._commands.items():
            keep = []
            for item in side.pending:
                if (now - item[0]).total_seconds() <= WINDOW_SECONDS:
                    keep.append(item)
                    continue
                record = item[2]
                purpose = str(record.get("purpose") or "lighting")
                self.mismatches += 1
                self.mismatch_by_purpose[purpose] += 1
                self.last_mismatch_at = item[0]
                entities = set(_entities(record.get("data") or {}))
                other = self._commands["integration" if who == "app" else "app"]
                self.discrepancies.append({
                    "at": iso(item[0]), "only": who, "purpose": purpose, "area_id": record.get("area_id"),
                    "command": _brief(record),
                    "other_side": [r for r in other.recent if entities & set(r.get("entities") or ())][-5:],
                })
            side.pending = keep
        for who, side in self._decisions.items():
            keep = []
            for item in side.pending:
                if (now - item[0]).total_seconds() <= WINDOW_SECONDS:
                    keep.append(item)
                    continue
                self.decision_differences += 1
                self.decision_log.append({"at": iso(item[0]), "only": who, **item[2]})
            side.pending = keep

    # --------------------------------------------------------------- report
    def report(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        self.sweep(now)
        return {
            "running": self.running,
            "started_at": iso(self.started_at),
            "seconds": round((now - self.started_at).total_seconds()) if self.running and self.started_at else 0,
            "window_seconds": WINDOW_SECONDS,
            "commands": {"matched": self.matched, "mismatches": self.mismatches,
                         "by_purpose": dict(self.by_purpose), "mismatch_by_purpose": dict(self.mismatch_by_purpose),
                         "pending": {k: len(v.pending) for k, v in self._commands.items()},
                         "last_match_at": iso(self.last_match_at), "last_mismatch_at": iso(self.last_mismatch_at)},
            "decisions": {"matched": self.decisions_matched, "differences": self.decision_differences},
            "discrepancies": list(reversed(self.discrepancies)),
            "decision_differences": list(reversed(self.decision_log)),
            "recent": {k: list(reversed(v.recent))[:20] for k, v in self._commands.items()},
        }

    def persist(self) -> dict[str, Any]:
        return {"version": 1, "matched": self.matched, "mismatches": self.mismatches,
                "by_purpose": dict(self.by_purpose), "discrepancies": list(self.discrepancies),
                "decision_differences": list(self.decision_log)}


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    data = dict(record.get("data") or {})
    entities = list(_entities(data))
    data.pop("entity_id", None)
    return {"purpose": record.get("purpose"), "area_id": record.get("area_id"),
            "service": f"{record.get('domain')}.{record.get('service')}", "entities": entities, "data": data,
            "error": record.get("error")}


def _decision_brief(decision: dict[str, Any]) -> dict[str, Any]:
    return {k: decision.get(k) for k in ("at", "kind", "trigger", "status", "target", "action", "explanation",
                                         "mode")}
