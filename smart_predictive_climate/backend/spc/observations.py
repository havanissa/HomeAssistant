"""Climate observations (Phase A7.0): the timestamped measurements thermal learning (A7.1) will learn from.

Storage (``/data/climate/observations``)::

    obs-YYYYMMDD.jsonl   one record per line, the UTC day of its timestamp (appended; never rewritten)
    state.json           per entity: the time range the App has observed (coverage) and known gaps

A record keeps the original measurement and its quality::

    {"t": 1795000000.123,            # when Home Assistant changed the state (epoch seconds, UTC)
     "e": "sensor.kamar_temperature", "k": "room_temp",
     "s": "21.4",                     # the original state, as Home Assistant reported it
     "u": "°C",                       # the original unit (temperatures)
     "v": 21.4,                       # the value in °C (or 1/0 for open/occupied), null when not usable
     "q": "ok",                       # ok | unavailable | unknown | not_numeric | wrong_unit | implausible
     "a": {...},                      # kind-specific fields (thermostat, sun, cover), see ``sample``
     "src": "live"}                   # live (a state change) | report (an unchanged value re-confirmed)
                                      # | recorder (read from the Recorder's history to fill a gap)

Thermostat records keep what the device *reports*: ``a.action`` is
``hvac_action`` when the entity has it and ``null`` otherwise (never derived
from the setpoint). ``a.cur`` is the thermostat's own measurement, separate
from the room's sensors.

"stale" is not a stored quality: it depends on when a record is read, and a
reader derives it from the gaps between records (``report`` records are
written at most every ``report_minutes`` while a value does not change, so a
silent sensor shows up as a gap). Coverage says when the App was listening;
outside it, the absence of records means *not observed*, not *unchanged*.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any

from .units import PLAUSIBLE, as_float, climate_unit, to_celsius

_LOGGER = logging.getLogger(__name__)

KINDS = ("room_temp", "thermostat", "outdoor", "opening", "occupancy", "sun", "cover", "illuminance", "weather",
         "humidity", "irradiance", "wind", "energy", "running", "presence")
#: Kinds whose state is simply a non-negative number (stored as it is, with its unit).
NUMERIC_KINDS = ("illuminance", "humidity", "irradiance", "wind", "energy")
#: The least time between two records of a kind whose attributes move continuously (seconds).
MIN_INTERVAL = {"sun": 600.0}
MAX_STORE_BYTES = 300 * 1024 * 1024
MAX_GAPS = 1000  # A7.1: was 50; a forgotten gap would look observed to the learner
MAX_READ_DAYS = 31
SEED_LOOKBACK_DAYS = 2


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y%m%d")


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _num(value: Any, digits: int = 2) -> float | None:
    number = as_float(value)
    return None if number is None else round(number, digits)


def _temp(value: Any, unit: str) -> float | None:
    c = to_celsius(value, unit)
    return None if c is None else round(c, 2)


def sample(kind: str, entity_id: str, state: dict[str, Any] | None, *, ha_unit: str, src: str,
           t: float | None = None) -> dict[str, Any] | None:
    """A record from a Home Assistant state (a live state, or a Recorder row expanded to the same shape)."""
    if state is None:
        return None
    raw = state.get("state")
    attrs = state.get("attributes") or {}
    when = t if t is not None else _ts(state.get("last_updated")) or _ts(state.get("last_changed"))
    if when is None:
        return None
    rec: dict[str, Any] = {"t": round(when, 3), "e": entity_id, "k": kind, "s": None if raw is None else str(raw)[:64],
                           "v": None, "q": "ok", "src": src}
    if raw == "unavailable":
        rec["q"] = "unavailable"
    elif raw in (None, "unknown", ""):
        rec["q"] = "unknown"
    if kind in ("room_temp", "outdoor"):
        if entity_id.startswith("weather."):
            unit = attrs.get("temperature_unit") or ha_unit
            value = attrs.get("temperature")
        else:
            unit, value = attrs.get("unit_of_measurement") or "", raw
        rec["u"] = unit
        if rec["q"] == "ok":
            if as_float(value) is None:
                rec["q"] = "not_numeric"
            elif (c := _temp(value, unit)) is None:
                rec["q"] = "wrong_unit"
            else:
                rec["v"] = c
                lo, hi = PLAUSIBLE["outdoor" if kind == "outdoor" else "indoor"]
                if not lo <= c <= hi:
                    rec["q"] = "implausible"
    elif kind == "thermostat":
        ha_unit = climate_unit(attrs, ha_unit)
        rec["u"] = ha_unit
        rec["a"] = {
            "cur": _temp(attrs.get("current_temperature"), ha_unit),
            "tgt": _temp(attrs.get("temperature"), ha_unit),
            "lo": _temp(attrs.get("target_temp_low"), ha_unit),
            "hi": _temp(attrs.get("target_temp_high"), ha_unit),
            # Reported activity only: null when the device has no hvac_action.
            "action": str(attrs["hvac_action"])[:20] if attrs.get("hvac_action") is not None else None,
            "preset": str(attrs["preset_mode"])[:40] if attrs.get("preset_mode") is not None else None,
        }
        rec["v"] = rec["a"]["cur"] if rec["q"] == "ok" else None
    elif kind in ("opening", "occupancy"):
        if rec["q"] == "ok":
            if raw in ("on", "off"):
                rec["v"] = 1 if raw == "on" else 0
            else:
                rec["q"] = "not_numeric"
    elif kind == "sun":
        rec["a"] = {"elevation": _num(attrs.get("elevation"), 1), "azimuth": _num(attrs.get("azimuth"), 1)}
        rec["v"] = rec["a"]["elevation"]
    elif kind == "weather":
        # A weather entity: its measured outdoor temperature and cloud coverage (the state is the condition).
        unit = attrs.get("temperature_unit") or ha_unit
        rec["u"] = unit
        cloud = _num(attrs.get("cloud_coverage"), 0)
        rec["a"] = {"temp": _temp(attrs.get("temperature"), unit), "cloud": cloud if cloud is not None and 0 <= cloud <= 100 else None,
                    "wind": _num(attrs.get("wind_speed"), 1), "wind_bearing": _num(attrs.get("wind_bearing"), 0),
                    "humidity": _num(attrs.get("humidity"), 0)}
        rec["v"] = rec["a"]["temp"] if rec["q"] == "ok" else None
        if rec["q"] == "ok" and rec["v"] is not None and not PLAUSIBLE["outdoor"][0] <= rec["v"] <= PLAUSIBLE["outdoor"][1]:
            rec["q"] = "implausible"
    elif kind in ("running", "presence"):
        # A heating system, boiler or pump that runs (on/off), or somebody being home (on/off, home/not_home).
        if rec["q"] == "ok":
            on = {"on": 1, "off": 0, "home": 1, "not_home": 0, "heat": 1, "heating": 1, "idle": 0}.get(str(raw))
            if on is None:
                rec["q"] = "not_numeric"
            else:
                rec["v"] = on
    elif kind in NUMERIC_KINDS:
        # Plain non-negative measurements: sunlight (lux), irradiance, humidity, wind, energy or power.
        rec["u"] = attrs.get("unit_of_measurement") or ""
        if rec["q"] == "ok":
            number = as_float(raw)
            if number is None:
                rec["q"] = "not_numeric"
            elif number < 0:
                rec["q"] = "implausible"
            else:
                rec["v"] = round(number, 1)
    elif kind == "cover":
        rec["a"] = {"position": _num(attrs.get("current_position"), 0)}
        rec["v"] = rec["a"]["position"]
    return rec


def signature(rec: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two records of an entity different (its time and source do not)."""
    return rec.get("s"), rec.get("v"), rec.get("q"), json.dumps(rec.get("a"), sort_keys=True)


def history_rows(kind: str, entity_id: str, rows: Any, *, ha_unit: str, after: float, before: float
                 ) -> list[dict[str, Any]]:
    """Records from the Recorder's compressed history (``s``, ``a``, ``lu``, ``lc``), inside (after, before)."""
    out: list[dict[str, Any]] = []
    last_sig = None
    last_t = -1e18
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        t = _ts(row.get("lu", row.get("last_updated"))) or _ts(row.get("lc", row.get("last_changed")))
        if t is None or not after <= t < before:
            continue
        state = {"state": row.get("s", row.get("state")), "attributes": row.get("a", row.get("attributes")) or {}}
        rec = sample(kind, entity_id, state, ha_unit=ha_unit, src="recorder", t=t)
        if rec is None:
            continue
        sig = signature(rec)
        if sig == last_sig:
            continue
        if kind in MIN_INTERVAL and t - last_t < MIN_INTERVAL[kind] and last_sig and last_sig[0] == sig[0]:
            continue
        out.append(rec)
        last_sig, last_t = sig, t
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class ObservationStore:
    """Append-only observation segments with per-entity coverage. Blocking methods run in a worker thread."""

    def __init__(self, directory: Path | None, *, max_bytes: int = MAX_STORE_BYTES) -> None:
        self.dir = directory
        self.max_bytes = max_bytes
        self.coverage: dict[str, dict[str, float]] = {}
        self.gaps: dict[str, list[list[float]]] = {}
        self.aliases: dict[str, list[str]] = {}  # entity id -> its earlier ids (renamed): their records are its own
        self.error: str | None = None if directory else "The App has no data folder"
        self.written = 0
        self._buffer: list[dict[str, Any]] = []

    @property
    def writable(self) -> bool:
        return self.dir is not None and self.error is None

    def load(self) -> None:
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self.error = f"The observation folder cannot be created ({err})"
            _LOGGER.error("Climate observations are not stored: %s", err)
            return
        for leftover in self.dir.glob(".*.tmp"):
            with contextlib.suppress(OSError):
                leftover.unlink()
        path = self.dir / "state.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("The observation state could not be read (%s); coverage starts again", err)
            return
        for entity_id, span in (data.get("coverage") or {}).items():
            if isinstance(span, dict) and _ts(span.get("from")) is not None and _ts(span.get("to")) is not None:
                self.coverage[entity_id] = {"from": float(span["from"]), "to": float(span["to"])}
        for entity_id, old in (data.get("aliases") or {}).items():
            if isinstance(old, list):
                self.aliases[entity_id] = [o for o in old if isinstance(o, str)][:20]
        for entity_id, gaps in (data.get("gaps") or {}).items():
            if isinstance(gaps, list):
                self.gaps[entity_id] = [[float(a), float(b)] for a, b in
                                        (g for g in gaps if isinstance(g, list) and len(g) == 2)][-MAX_GAPS:]

    def save_state(self) -> None:
        if not self.writable:
            return
        data = {"version": 1, "coverage": self.coverage, "gaps": self.gaps, "aliases": self.aliases}
        try:
            _atomic_write(self.dir / "state.json", json.dumps(data, separators=(",", ":")).encode())  # type: ignore[operator]
        except OSError as err:
            _LOGGER.warning("Could not store the observation state: %s", err)

    # ---------------------------------------------------------------- writing
    def append(self, rec: dict[str, Any]) -> None:
        if self.writable:
            self._buffer.append(rec)

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def flush(self) -> int:
        """Write the buffered records to their day files. Returns how many were written."""
        if not self._buffer or not self.writable:
            self._buffer.clear()
            return 0
        records, self._buffer = self._buffer, []
        by_day: dict[str, list[str]] = {}
        for rec in records:
            by_day.setdefault(_day(rec["t"]), []).append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
        try:
            for day, lines in by_day.items():
                path = self.dir / f"obs-{day}.jsonl"  # type: ignore[operator]
                lead = ""
                with contextlib.suppress(OSError):
                    if path.stat().st_size:
                        with open(path, "rb") as tail:
                            tail.seek(-1, os.SEEK_END)
                            lead = "" if tail.read(1) == b"\n" else "\n"  # a line cut short by a power loss
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(lead + "\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as err:
            _LOGGER.warning("Could not store %d climate observations: %s", len(records), err)
            return 0
        self.written += len(records)
        return len(records)

    def mark(self, entity_id: str, start: float, end: float) -> None:
        span = self.coverage.get(entity_id)
        if span is None:
            self.coverage[entity_id] = {"from": start, "to": end}
        else:
            span["from"] = min(span["from"], start)
            span["to"] = max(span["to"], end)

    def add_gap(self, entity_id: str, start: float, end: float) -> None:
        if end - start < 60:
            return
        gaps = self.gaps.setdefault(entity_id, [])
        gaps.append([round(start, 1), round(end, 1)])
        del gaps[:-MAX_GAPS]

    def fill_gap(self, entity_id: str, start: float, end: float) -> None:
        """[start, end) was read from the Recorder: no longer a gap (what remains of a gap stays)."""
        gaps = self.gaps.get(entity_id)
        if not gaps:
            return
        out: list[list[float]] = []
        for a, b in gaps:
            if b <= start or a >= end:
                out.append([a, b])
                continue
            if a < start and start - a >= 60:
                out.append([a, start])
            if b > end and b - end >= 60:
                out.append([end, b])
        if out:
            self.gaps[entity_id] = out
        else:
            self.gaps.pop(entity_id, None)

    def forget(self, entity_ids: set[str]) -> None:
        """No longer tracked: their coverage ends (the stored records stay until retention removes them)."""
        for entity_id in entity_ids:
            self.coverage.pop(entity_id, None)
            self.gaps.pop(entity_id, None)

    def rename(self, old: str, new: str) -> None:
        if old in self.coverage and new not in self.coverage:
            self.coverage[new] = self.coverage.pop(old)
        if old in self.gaps:
            self.gaps.setdefault(new, []).extend(self.gaps.pop(old))
        # The records keep the id they were stored with; reading the new id also reads the old ones.
        earlier = [old, *self.aliases.pop(old, [])]
        self.aliases[new] = [e for e in dict.fromkeys([*self.aliases.get(new, []), *earlier]) if e != new][:20]

    # -------------------------------------------------------------- retention
    def files(self) -> list[Path]:
        if self.dir is None or not self.dir.is_dir():
            return []
        return sorted(self.dir.glob("obs-*.jsonl"))

    def prune(self, retention_days: int, now: float) -> int:
        """Remove day files older than the retention, then the oldest while the store is over its size limit."""
        removed = 0
        cutoff = _day(now - retention_days * 86400)
        files = self.files()
        for path in files:
            if path.stem[4:] < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        files = self.files()
        total = sum(p.stat().st_size for p in files if p.exists())
        while files[:-1] and total > self.max_bytes:
            oldest = files.pop(0)
            with contextlib.suppress(OSError):
                total -= oldest.stat().st_size
                oldest.unlink()
                removed += 1
        if removed:
            _LOGGER.info("Removed %d climate observation day files (retention %d days)", removed, retention_days)
        return removed

    def stats(self) -> dict[str, Any]:
        files = self.files()
        size = 0
        for p in files:
            with contextlib.suppress(OSError):
                size += p.stat().st_size
        first = files[0].stem[4:] if files else None
        return {"files": len(files), "bytes": size, "first_day": first, "last_day": files[-1].stem[4:] if files else None,
                "written": self.written, "pending": len(self._buffer), "writable": self.writable, "error": self.error}

    # ---------------------------------------------------------------- reading
    def read(self, entity_ids: set[str], start: float, end: float) -> dict[str, list[dict[str, Any]]]:
        """Each entity's records in [start, end), sorted, preceded by its last record before ``start`` (the
        value at the window's start), looked up at most two days back. Buffered records are included."""
        out: dict[str, list[dict[str, Any]]] = {e: [] for e in entity_ids}
        seed: dict[str, dict[str, Any]] = {}
        alias = {old: e for e in entity_ids for old in self.aliases.get(e, []) if old not in entity_ids}
        first = datetime.fromtimestamp(start, UTC).date() - timedelta(days=SEED_LOOKBACK_DAYS)
        last = datetime.fromtimestamp(end, UTC).date()
        if (last - first).days > MAX_READ_DAYS + SEED_LOOKBACK_DAYS:
            first = last - timedelta(days=MAX_READ_DAYS + SEED_LOOKBACK_DAYS)
        days = {(first + timedelta(days=i)).strftime("%Y%m%d") for i in range((last - first).days + 1)}

        def take(rec: dict[str, Any]) -> None:
            e = rec.get("e")
            if e in alias:
                e = alias[e]
                rec = {**rec, "e": e}
            t = rec.get("t")
            if e not in out or not isinstance(t, (int, float)):
                return
            if t < start:
                if e not in seed or seed[e]["t"] <= t:
                    seed[e] = rec
            elif t < end:
                out[e].append(rec)

        for path in self.files():
            if path.stem[4:] not in days:
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            take(json.loads(line))
                        except ValueError:
                            continue  # a line cut short by a power loss
            except OSError:
                continue
        for rec in self._buffer:
            take(rec)
        for e, records in out.items():
            records.sort(key=lambda r: r["t"])
            if e in seed:
                records.insert(0, seed[e])
        return out


# ------------------------------------------------------------ chart shaping
def numeric_series(records: list[dict[str, Any]], *, start: float, end: float, field: str | None = None,
                   buckets: int = 480, step: bool = False, gaps: list[list[float]] | None = None) -> list[list[Any]]:
    """[[t, value|null], ...] for a chart: the mean of the usable readings per time bucket (or every change for a
    step series). Every unusable reading (unavailable, unknown, implausible ...) adds a null point at its own time,
    so the line breaks exactly there and stays broken until the next usable reading: a gap is never averaged
    away or bridged."""
    points: list[tuple[float, float | None]] = []
    for rec in records:
        value = (rec.get("a") or {}).get(field) if field else rec.get("v")
        usable = rec.get("q") == "ok" and isinstance(value, (int, float))
        points.append((max(rec["t"], start), float(value) if usable else None))
    # A recorded gap (not observed) breaks the line at its start, like an unusable reading.
    gap_starts = [(max(a, start), None) for a, b in gaps or [] if b > start and a < end]
    if gap_starts:
        points = sorted([*points, *gap_starts], key=lambda p: p[0])
    if step:
        out: list[list[Any]] = []
        for t, v in points:
            if out and out[-1][1] == v:
                continue
            out.append([round(t, 1), v])
        return out
    width = max(1.0, (end - start) / buckets)
    out = []
    times: list[float] = []
    values: list[float] = []
    current: int | None = None

    def flush() -> None:
        if values:
            out.append([round(sum(times) / len(times), 1), round(sum(values) / len(values), 2)])
        times.clear()
        values.clear()

    for t, v in points:
        index = int((t - start) // width)
        if current is not None and index != current:
            flush()
        current = index
        if v is None:
            flush()
            out.append([round(t, 1), None])
        else:
            times.append(t)
            values.append(v)
    flush()
    return out


def intervals(records: list[dict[str, Any]], *, start: float, end: float, active: Any,
              gaps: list[list[float]] | None = None) -> list[list[float]]:
    """[[from, to], ...] while ``active(record)`` holds, clipped to the window; never across a recorded gap."""
    out: list[list[float]] = []
    opened: float | None = None
    for rec in records:
        on = bool(active(rec))
        t = max(rec["t"], start)
        if on and opened is None:
            opened = t
        elif not on and opened is not None:
            if t > opened:
                out.append([round(opened, 1), round(t, 1)])
            opened = None
    if opened is not None and end > opened:
        out.append([round(opened, 1), round(end, 1)])
    for a, b in gaps or []:
        cut: list[list[float]] = []
        for x, y in out:
            if y <= a or x >= b:
                cut.append([x, y])
                continue
            if x < a:
                cut.append([x, round(a, 1)])
            if y > b:
                cut.append([round(b, 1), y])
        out = cut
    return out
