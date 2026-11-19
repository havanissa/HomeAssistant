"""Comparison of the App's shadow curtain manager with the integration's (Phase A5).

Both managers run the same code on the same events; the integration's cover
commands and automatic decisions arrive over the curtain bridge, the shadow's
are recorded where they are made (``CurtainManager.command_observer`` and
``decision_observer``), never sent.

* **Commands** match when the same cover service, the same cover and the same
  target position appear on both sides within :data:`WINDOW_SECONDS`. A command
  left without a partner is a *mismatch* and is kept with the other side's recent
  commands for the same cover. Only command mismatches block a handover, because
  only commands move a cover.
* **Decisions** are the per-window status changes of the automatic rules (what
  the window decided: condition, preset, action, why it was prevented) and the
  decision log entries. They match on window, kind, action, code, condition and
  preset within :data:`DECISION_WINDOW_SECONDS` (the two managers' timers fire
  at nearly the same instant, not the same one). Differences are reported.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import UTC, datetime
import json
from typing import Any

WINDOW_SECONDS = 3.0
DECISION_WINDOW_SECONDS = 10.0
HISTORY = 100


def _now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def command_key(record: dict[str, Any]) -> str:
    data = dict(record.get("data") or {})
    return json.dumps([record.get("domain"), record.get("service"), data.get("entity_id"), data.get("position")],
                      sort_keys=True, default=str)


def decision_key(window_id: str, decision: dict[str, Any]) -> str:
    kind = decision.get("kind")
    if kind == "log":
        return json.dumps([window_id, "log", decision.get("action"), decision.get("condition"),
                           decision.get("preset_name")], default=str)
    return json.dumps([window_id, "status", decision.get("action"), decision.get("code"), decision.get("condition"),
                       decision.get("preset_id")], default=str)


class _Side:
    def __init__(self) -> None:
        self.pending: list[tuple[datetime, str, dict[str, Any]]] = []
        self.recent: deque[dict[str, Any]] = deque(maxlen=40)


class CurtainComparator:
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

    def start(self, now: datetime | None = None) -> None:
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
                self.by_purpose[str(record.get("purpose") or "automatic")] += 1
                self.last_match_at = now
                return
        mine.pending.append((now, key, record))

    def decision(self, who: str, window_id: str, decision: dict[str, Any], now: datetime | None = None) -> None:
        if not self.running:
            return
        now = now or _now()
        self.sweep(now)
        key = decision_key(window_id, decision)
        other = self._decisions["integration" if who == "app" else "app"]
        for i, (_, other_key, _) in enumerate(other.pending):
            if other_key == key:
                del other.pending[i]
                self.decisions_matched += 1
                return
        self._decisions[who].pending.append((now, key, {"window_id": window_id, **_decision_brief(decision)}))

    def sweep(self, now: datetime | None = None) -> None:
        now = now or _now()
        for who, side in self._commands.items():
            keep = []
            for item in side.pending:
                if (now - item[0]).total_seconds() <= WINDOW_SECONDS:
                    keep.append(item)
                    continue
                record = item[2]
                purpose = str(record.get("purpose") or "automatic")
                self.mismatches += 1
                self.mismatch_by_purpose[purpose] += 1
                self.last_mismatch_at = item[0]
                entity = (record.get("data") or {}).get("entity_id")
                other = self._commands["integration" if who == "app" else "app"]
                self.discrepancies.append({
                    "at": iso(item[0]), "only": who, "purpose": purpose, "window_id": record.get("window_id"),
                    "command": _brief(record),
                    "other_side": [r for r in other.recent if r.get("entity_id") == entity][-5:],
                })
            side.pending = keep
        for who, side in self._decisions.items():
            keep = []
            for item in side.pending:
                if (now - item[0]).total_seconds() <= DECISION_WINDOW_SECONDS:
                    keep.append(item)
                    continue
                self.decision_differences += 1
                self.decision_log.append({"at": iso(item[0]), "only": who, **item[2]})
            side.pending = keep

    def report(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or _now()
        self.sweep(now)
        return {
            "running": self.running,
            "started_at": iso(self.started_at),
            "seconds": round((now - self.started_at).total_seconds()) if self.running and self.started_at else 0,
            "window_seconds": WINDOW_SECONDS,
            "decision_window_seconds": DECISION_WINDOW_SECONDS,
            "commands": {"matched": self.matched, "mismatches": self.mismatches,
                         "by_purpose": dict(self.by_purpose), "mismatch_by_purpose": dict(self.mismatch_by_purpose),
                         "pending": {k: len(v.pending) for k, v in self._commands.items()},
                         "last_match_at": iso(self.last_match_at), "last_mismatch_at": iso(self.last_mismatch_at)},
            "decisions": {"matched": self.decisions_matched, "differences": self.decision_differences},
            "discrepancies": list(reversed(self.discrepancies)),
            "decision_differences": list(reversed(self.decision_log)),
            "recent": {k: list(reversed(v.recent))[:20] for k, v in self._commands.items()},
        }


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    data = dict(record.get("data") or {})
    return {"purpose": record.get("purpose"), "window_id": record.get("window_id"),
            "service": f"{record.get('domain')}.{record.get('service')}", "entity_id": data.get("entity_id"),
            "position": data.get("position"), "error": record.get("error")}


def _decision_brief(decision: dict[str, Any]) -> dict[str, Any]:
    return {k: decision.get(k) for k in ("at", "kind", "action", "word", "code", "condition", "preset_id",
                                         "preset_name", "text", "summary", "window")}
