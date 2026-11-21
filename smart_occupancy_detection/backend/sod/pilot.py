"""Pilot comparison (Phase 0.2): this App's occupancy next to a reference, usually Smart Comfort's occupancy entity.

Observation only. For each room with a reference entity the comparison follows both outputs; when they disagree
for longer than :data:`GRACE_SECONDS` (both known), a *difference* is recorded with its start and end, both
decisions with their reasons, the room's sensor evidence at the start, and a plain explanation. Nothing here
changes a decision, a rule or a timing: differences are explained, never adjusted.

Storage (separate from the room configuration, so a downgrade or a rollback never meets unknown fields):
``/data/pilot/settings.json`` (the reference per room) and ``/data/pilot/differences.json`` (bounded).
"""

from __future__ import annotations

import asyncio
from collections import deque
import csv
from datetime import UTC, datetime
import io
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from sod_engine.engine import SignalStatus, Value, interpret

from .const import SMART_COMFORT_DOMAIN
from .protocol import DOMAIN as OWN_DOMAIN

_LOGGER = logging.getLogger(__name__)

GRACE_SECONDS = 60
MAX_DIFFERENCES = 2000
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
REFERENCE_ATTRIBUTES = ("reason", "state", "confidence", "reason_code", "fresh")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def explain(ours: dict[str, Any], reference: dict[str, Any]) -> str:
    """Why the two may disagree, from what is visible here (Smart Comfort's own configuration is not readable)."""
    their_reason = (reference.get("attributes") or {}).get("reason")
    theirs = f" Smart Comfort says: {their_reason}" if their_reason else ""
    active = [s["name"] for s in ours.get("signals", []) if s["status"] in ("active", "grace", "recent")]
    unknown = [s["name"] for s in ours.get("signals", []) if s["status"] == "unknown"]
    if ours["occupied"] and not reference["occupied"]:
        if ours["state"] == "possible_vacancy":
            return ("This App is still waiting for its vacancy timeout (no sensor active); the reference already "
                    "decided vacant - a shorter timeout or exit evidence there." + theirs)
        if ours["state"] == "possible_occupancy":
            return (f"One uncorroborated movement here ({', '.join(active) or 'a sensor'}); the reference did not "
                    "react - it may not use this sensor." + theirs)
        if unknown and not active:
            return f"Held occupied here because {', '.join(unknown)} became unavailable while reporting presence." + theirs
        return f"Occupied here: {', '.join(active) or 'activity'}; the reference may not use these sensors." + theirs
    if reference["occupied"] and not ours["occupied"]:
        if ours["state"] == "possible_occupancy":
            return "Only a door moved here (a door alone does not prove presence); the reference counts it." + theirs
        return ("No sensor of this room is active here; the reference may use other sensors, a longer timeout, "
                "or still be confirming its vacancy." + theirs)
    return "Both outputs differ." + theirs


class Pilot:
    def __init__(self, ha: Any, runtime: Any, data_dir: Path | None) -> None:
        self.ha = ha
        self.runtime = runtime
        self.dir = data_dir / "pilot" if data_dir else None
        self.references: dict[str, str] = {}
        self.differences: deque[dict[str, Any]] = deque(maxlen=MAX_DIFFERENCES)
        self.stats: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}   # area -> disagreement waiting for the grace time
        self._open: dict[str, dict[str, Any]] = {}      # area -> recorded difference not ended yet
        self._last: dict[str, tuple[datetime, bool | None]] = {}  # area -> (time, agreed) of the last check
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._save_handle: asyncio.TimerHandle | None = None
        self._seq = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._load)
        self.runtime.add_listener(self._rooms_changed)
        self.ha.add_listener(self._on_ha)

    async def stop(self) -> None:
        for handle in self._timers.values():
            handle.cancel()
        if self._save_handle:
            self._save_handle.cancel()
        await asyncio.to_thread(self._save)

    def _load(self) -> None:
        if self.dir is None:
            return
        try:
            settings = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
            self.references = {a: e for a, e in (settings.get("references") or {}).items()
                               if isinstance(a, str) and isinstance(e, str) and ENTITY_ID.match(e)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as err:
            _LOGGER.warning("The pilot settings are not readable (%s); no comparison until set again", err)
        try:
            data = json.loads((self.dir / "differences.json").read_text(encoding="utf-8"))
            for item in data.get("differences") or []:
                if isinstance(item, dict):
                    self.differences.append(item)
            self.stats = {a: s for a, s in (data.get("stats") or {}).items() if isinstance(s, dict)}
            self._seq = max((d.get("seq", 0) for d in self.differences), default=0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as err:
            _LOGGER.warning("The pilot differences are not readable (%s); starting a new log", err)

    def _save(self) -> None:
        if self.dir is None:
            return
        _write(self.dir / "settings.json", {"schema": 1, "references": self.references})
        _write(self.dir / "differences.json", {"schema": 1, "differences": list(self.differences),
                                                "stats": self.stats})

    def _save_soon(self) -> None:
        if self._save_handle is None and self.dir is not None:
            def run() -> None:
                self._save_handle = None
                asyncio.get_running_loop().create_task(asyncio.to_thread(self._save))
            self._save_handle = asyncio.get_running_loop().call_later(10, run)

    # ------------------------------------------------------------ settings
    def suggestion(self, area_id: str) -> str | None:
        """Smart Comfort's occupancy entity of the same area, if it exists."""
        unique_id = f"smart_occupancy_room_{area_id}_occupied"
        entry = next((e for e in self.ha.entity_registry if e.get("platform") == SMART_COMFORT_DOMAIN
                      and e.get("unique_id") == unique_id), None)
        return entry.get("entity_id") if entry else None

    def set_reference(self, area_id: str, entity_id: str | None) -> None:
        if entity_id in (None, ""):
            self.references.pop(area_id, None)
            self._close(area_id, utcnow(), "comparison turned off")
        else:
            if not isinstance(entity_id, str) or not ENTITY_ID.match(entity_id):
                raise ValueError("not a valid entity id")
            entry = next((e for e in self.ha.entity_registry if e.get("entity_id") == entity_id), None)
            if entry and entry.get("platform") == OWN_DOMAIN:
                raise ValueError("this App's own output cannot be its reference")
            self.references[area_id] = entity_id
            self._last.pop(area_id, None)
            self._check(area_id)
        self._save_soon()

    # ------------------------------------------------------------ events
    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            entity_id = payload[0]
            for area_id, reference in list(self.references.items()):
                if reference == entity_id:
                    self._check(area_id)

    def _rooms_changed(self, area_ids: list[str]) -> None:
        for area_id in area_ids:
            if area_id in self.references:
                self._check(area_id)

    def _ours(self, area_id: str) -> dict[str, Any] | None:
        result = self.runtime.result(area_id)
        if result is None or self.runtime.suspended:
            return None
        return {"state": result.state.value, "occupied": result.occupied, "reason": result.reason,
                "reason_code": result.reason_code, "last_activity": _iso(result.last_activity),
                "signals": [{"entity_id": v.track.entity_id, "name": v.track.label(), "value": v.track.value.value,
                             "status": v.status.value, "since": _iso(v.track.since)} for v in result.signals]}

    def _reference(self, area_id: str) -> dict[str, Any] | None:
        entity_id = self.references.get(area_id)
        state = self.ha.states.get(entity_id or "")
        if not state:
            return None
        value = interpret(state.get("state"))
        attributes = state.get("attributes") or {}
        return {"entity_id": entity_id, "state": state.get("state"),
                "occupied": None if value == Value.UNKNOWN else value == Value.ACTIVE,
                "attributes": {k: attributes[k] for k in REFERENCE_ATTRIBUTES if k in attributes},
                "last_changed": state.get("last_changed")}

    def _check(self, area_id: str) -> None:
        now = utcnow()
        ours = self._ours(area_id)
        reference = self._reference(area_id)
        comparable = ours is not None and reference is not None and ours["state"] != "uncertain" \
            and ours["occupied"] is not None and reference["occupied"] is not None
        agreed = None if not comparable else ours["occupied"] == reference["occupied"]
        self._account(area_id, now, agreed)
        if agreed is None or agreed:
            self._pending.pop(area_id, None)
            if handle := self._timers.pop(area_id, None):
                handle.cancel()
            if agreed:
                self._close(area_id, now, "agree again")
            return
        if area_id in self._open:
            return
        pending = self._pending.get(area_id)
        if pending is None:
            self._pending[area_id] = {"since": now, "ours": ours, "reference": reference}
            self._timers[area_id] = asyncio.get_running_loop().call_later(
                GRACE_SECONDS + 0.5, self._check, area_id)
        elif (now - pending["since"]).total_seconds() >= GRACE_SECONDS:
            self._seq += 1
            record = {"seq": self._seq, "area_id": area_id, "room": self.runtime.room_name(area_id),
                      "started_at": _iso(pending["since"]), "ended_at": None, "seconds": None,
                      "ours": {k: v for k, v in pending["ours"].items()},
                      "reference": pending["reference"], "explanation": explain(pending["ours"], pending["reference"])}
            self._open[area_id] = record
            self.differences.append(record)
            stats = self.stats.setdefault(area_id, {})
            stats["differences"] = stats.get("differences", 0) + 1
            self._pending.pop(area_id, None)
            _LOGGER.info("Pilot difference in %s: this App %s, reference %s", area_id,
                         "occupied" if pending["ours"]["occupied"] else "vacant",
                         "occupied" if pending["reference"]["occupied"] else "vacant")
            self._save_soon()

    def _close(self, area_id: str, now: datetime, why: str) -> None:
        record = self._open.pop(area_id, None)
        if record is not None:
            started = datetime.fromisoformat(record["started_at"])
            record["ended_at"] = _iso(now)
            record["seconds"] = int((now - started).total_seconds())
            record["ended_because"] = why
            self._save_soon()

    def _account(self, area_id: str, now: datetime, agreed: bool | None) -> None:
        """Time in agreement and in disagreement (only while both outputs are known)."""
        stats = self.stats.setdefault(area_id, {"agree_seconds": 0, "disagree_seconds": 0, "differences": 0,
                                                "since": _iso(now)})
        last = self._last.get(area_id)
        if last is not None and last[1] is not None:
            key = "agree_seconds" if last[1] else "disagree_seconds"
            stats[key] = stats.get(key, 0) + max(0.0, (now - last[0]).total_seconds())
        self._last[area_id] = (now, agreed)

    # ------------------------------------------------------------ views
    def room(self, area_id: str) -> dict[str, Any] | None:
        if area_id not in self.references:
            return None
        stats = dict(self.stats.get(area_id) or {})
        total = (stats.get("agree_seconds") or 0) + (stats.get("disagree_seconds") or 0)
        current = self._reference(area_id)
        ours = self._ours(area_id)
        return {
            "reference": self.references[area_id], "reference_state": current["state"] if current else None,
            "agreement": round(100 * (stats.get("agree_seconds") or 0) / total, 1) if total else None,
            "compared_seconds": int(total), "differences": stats.get("differences", 0), "since": stats.get("since"),
            "now": None if not (ours and current) else (
                "agree" if ours["occupied"] == current["occupied"] else "differ"),
            "pending_since": _iso(self._pending[area_id]["since"]) if area_id in self._pending else None,
            "recent": [d for d in reversed(self.differences) if d["area_id"] == area_id][:20],
        }

    def overview(self) -> dict[str, Any]:
        rooms = {}
        for area_id in (self.runtime.store.config.get("rooms") or {}):
            rooms[area_id] = {"name": self.runtime.room_name(area_id), "reference": self.references.get(area_id),
                              "suggestion": self.suggestion(area_id), "summary": self.room(area_id)}
        return {"grace_seconds": GRACE_SECONDS, "rooms": rooms}

    def report(self) -> list[dict[str, Any]]:
        return list(self.differences)

    def report_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["room", "started_at", "ended_at", "seconds", "this_app", "this_app_state", "this_app_reason",
                         "reference", "reference_state", "reference_reason", "active_sensors", "unavailable_sensors",
                         "explanation"])
        for d in self.differences:
            ours, ref = d["ours"], d["reference"]
            writer.writerow([
                d["room"], d["started_at"], d["ended_at"] or "", d["seconds"] if d["seconds"] is not None else "",
                "occupied" if ours["occupied"] else "vacant", ours["state"], ours["reason"],
                "occupied" if ref["occupied"] else "vacant", ref["state"], (ref.get("attributes") or {}).get("reason", ""),
                "; ".join(s["name"] for s in ours["signals"] if s["status"] in (
                    SignalStatus.ACTIVE.value, SignalStatus.GRACE.value, SignalStatus.RECENT.value)),
                "; ".join(s["name"] for s in ours["signals"] if s["status"] == SignalStatus.UNKNOWN.value),
                d["explanation"]])
        return out.getvalue()
