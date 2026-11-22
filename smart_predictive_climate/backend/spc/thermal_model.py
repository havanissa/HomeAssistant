"""Climate thermal learning (Phase A7.1): the room thermal model, its stored form and the A7.2 interface.

Pure code (no I/O). The fitting engine (``climate_fit``) produces a model document; this module defines and
checks that document, turns it into a ``ThermalModel`` and evaluates it. A7.2 (prediction and simulation) is
meant to use **only** this module: ``load_model``, ``simulate``, ``estimate_change`` and ``evaluate``. The fit's
own validation runs through the same ``step`` function, so what A7.2 computes is what was validated.

The model
---------
A constrained first-order (one thermal mass) model per room, on a 5-minute step::

    dT/dt = k · (T_ref − T)            loss towards the reference (°C/h)
          + b · u_f                    heating: b °C/h at full heating, u_f = the reported heating fraction
                                       (0..1), delayed by ``lag`` minutes and smoothed by the emitter's own
                                       time constant ``tau`` (radiators keep warming after they stop)
          + a · s                      direct sun: a °C/h at a direct-sun index s of 1 (sun head-on on the glass,
                                       covers open, as bright as observed)
          + n · night                  a different baseline at night (22–06 local), only when the data shows it
          + Σ c_j · (T_j − T)          exchange with the other rooms of the thermal zone, where measurable
          + d                          baseline gain (°C/h): heat from the rest of the house, people, appliances

``T_ref`` is the outdoor temperature when an outdoor source is configured; without one it is a fixed
temperature (the median room temperature the model was trained on) and ``k`` is only a relaxation rate, not an
outdoor coefficient. Every coefficient except ``n`` and ``d`` is ≥ 0, and all have upper bounds
(``BOUNDS``): an estimate at a bound is rejected, never stored. A term the data could not identify is absent
(its parameter says why), so it contributes nothing.

Units: temperatures °C, time hours (``*_per_h``), ``lag``/``tau`` minutes.

Stored as ``/data/climate/models/<area_id>.json``::

    {"format": "smart_comfort.thermal_model", "schema": 1, "algorithm": "a7.1-first-order/1",
     "area_id", "trained_at", "app_version", "config_revision", "fingerprint": {...}, "fingerprint_hash",
     "status": "low" | "moderate" | "high" | "insufficient" | "unidentifiable", "preliminary": bool,
     "structure": {"reference": "outdoor" | "fixed", "reference_c": float | None, "heating": "reported" |
                   "inferred" | None, "solar": bool, "night": bool, "coupling": [area ids]},
     "parameters": {name: {"value", "se", "unit", "confidence", "available", "basis", "reason", "text"}},
     "dynamics": {"lag_min", "tau_min"},
     "data": {...range, train/test split, grid...}, "evidence": {...}, "validation": {...},
     "confidence": {"level", "score", "factors": [...]}, "uncertainty": {"60": {...}, "180": {...}},
     "explanations": [...], "charts": {...}}
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math
from typing import Any

FORMAT = "smart_comfort.thermal_model"
SCHEMA = 1
ALGORITHM = "a7.1-first-order/1"
STEP_MIN = 5.0
STATUSES = ("low", "moderate", "high", "insufficient", "unidentifiable")
CONFIDENCE = ("none", "low", "moderate", "high")
#: Physically plausible ranges. A fitted value outside (or on the edge of) its range is not accepted.
BOUNDS: dict[str, tuple[float, float]] = {
    "k_per_h": (0.0, 1.0),            # time constant ≥ 1 h for a closed room
    "heating_c_per_h": (0.0, 8.0),
    "solar_c_per_h": (0.0, 8.0),
    "night_c_per_h": (-2.0, 2.0),
    "coupling_per_h": (0.0, 2.0),
    "baseline_c_per_h": (-4.0, 8.0),
}
#: Beyond the longest validated horizon the uncertainty band grows with the square root of time and is flagged.
HORIZONS_MIN = (60, 180, 360)


class ModelError(Exception):
    """A model document that is not valid (``code``: invalid, newer_schema, other_algorithm)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Unavailable:
    """A structured "cannot answer": why, and which inputs or evidence are missing."""

    code: str
    text: str
    missing: tuple[str, ...] = ()
    available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"available": False, "code": self.code, "text": self.text, "missing": list(self.missing)}


@dataclass(frozen=True)
class ThermalModel:
    area_id: str
    trained_at: str
    status: str
    preliminary: bool
    reference: str                        # outdoor | fixed
    reference_c: float | None
    k: float
    d: float
    heating: float | None                 # None: no heating term
    heating_basis: str | None             # reported | inferred
    lag_min: float
    tau_min: float
    solar: float | None
    night: float | None
    coupling: dict[str, float]
    uncertainty: dict[int, dict[str, float]]
    confidence: str
    fingerprint_hash: str | None
    warnings: tuple[str, ...] = ()

    @property
    def time_constant_h(self) -> float | None:
        rate = self.k + sum(self.coupling.values())
        return 1.0 / rate if rate > 1e-9 else None


@dataclass(frozen=True)
class Inputs:
    """The conditions during one step. ``heating`` is the heating fraction 0..1 (already delayed by the caller
    for ``step``; ``simulate`` applies the model's delay itself)."""

    outdoor_c: float | None = None
    heating: float | None = 0.0
    solar: float | None = None
    night: bool | None = None
    neighbours: dict[str, float] | None = None


@dataclass(frozen=True)
class Trajectory:
    """Temperatures every ``step_min`` minutes from the start (index 0 = the start), with an uncertainty band
    (``low``/``high``: the 80 % band from the held-out validation errors). A generated prediction, never an
    observation."""

    start_c: float
    step_min: float
    temps: tuple[float, ...]
    low: tuple[float, ...]
    high: tuple[float, ...]
    warnings: tuple[str, ...]
    available: bool = True
    kind: str = "model_estimate"

    def as_dict(self) -> dict[str, Any]:
        return {"available": True, "kind": self.kind, "start_c": self.start_c, "step_min": self.step_min,
                "temps": [round(t, 3) for t in self.temps], "low": [round(t, 3) for t in self.low],
                "high": [round(t, 3) for t in self.high], "warnings": list(self.warnings)}


# ------------------------------------------------------------- documents
def check_document(doc: Any) -> None:
    """Raise ModelError unless ``doc`` is a model this version can read."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ModelError("invalid", "Not a Smart Comfort thermal model")
    schema = doc.get("schema")
    if not isinstance(schema, int) or schema < 1:
        raise ModelError("invalid", "Unknown model schema")
    if schema > SCHEMA:
        raise ModelError("newer_schema", f"This model was written by a newer App (model schema {schema}); update the "
                         "App to use it")
    if not isinstance(doc.get("area_id"), str) or not isinstance(doc.get("trained_at"), str):
        raise ModelError("invalid", "The model has no room or training time")
    if doc.get("status") not in STATUSES:
        raise ModelError("invalid", "Unknown model status")
    structure = doc.get("structure")
    params = doc.get("parameters")
    if not isinstance(structure, dict) or not isinstance(params, dict):
        raise ModelError("invalid", "The model has no structure or parameters")
    if structure.get("reference") not in ("outdoor", "fixed"):
        raise ModelError("invalid", "Unknown model reference")
    if structure["reference"] == "fixed" and doc.get("status") != "unidentifiable" and (
            isinstance(structure.get("reference_c"), bool) or not isinstance(structure.get("reference_c"), (int, float))):
        raise ModelError("invalid", "A fixed-reference model needs its reference temperature")
    dyn = doc.get("dynamics") or {}
    for key, hi in (("lag_min", 240), ("tau_min", 600)):
        value = dyn.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= hi:
            raise ModelError("invalid", f"dynamics.{key} must be between 0 and {hi} minutes")
    for name, p in params.items():
        if not isinstance(p, dict):
            raise ModelError("invalid", f"Parameter {name} is not valid")
        value = p.get("value")
        if p.get("available"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ModelError("invalid", f"Parameter {name} has no finite value")
            bound = BOUNDS.get(p.get("bound") or name)
            if bound is not None and not bound[0] <= value <= bound[1]:
                raise ModelError("invalid", f"Parameter {name} = {value} is outside its plausible range")


def _value(params: dict[str, Any], name: str) -> float | None:
    p = params.get(name)
    if isinstance(p, dict) and p.get("available") and isinstance(p.get("value"), (int, float)):
        return float(p["value"])
    return None


def from_document(doc: dict[str, Any]) -> ThermalModel:
    check_document(doc)
    s = doc["structure"]
    p = doc["parameters"]
    dyn = doc.get("dynamics") or {}
    coupling = {}
    for area in s.get("coupling") or []:
        value = _value(p, f"coupling:{area}")
        if value is not None:
            coupling[str(area)] = value
    unc: dict[int, dict[str, float]] = {}
    for key, q in (doc.get("uncertainty") or {}).items():
        try:
            unc[int(key)] = {k: float(v) for k, v in q.items() if isinstance(v, (int, float))}
        except (TypeError, ValueError):
            continue
    heating = _value(p, "heating_c_per_h") if s.get("heating") else None
    if heating is None and s.get("heating") == "inferred":
        heating = _value(p, "heating_inferred_c_per_h")
    k = _value(p, "k_per_h")
    if k is None and s["reference"] == "fixed":
        k = 0.0  # the heat loss could not be measured without an outdoor temperature (no relaxation)
    if k is None:
        raise ModelError("invalid", "The model has no loss coefficient")
    return ThermalModel(
        area_id=doc["area_id"], trained_at=doc["trained_at"], status=doc["status"],
        preliminary=bool(doc.get("preliminary")), reference=s["reference"],
        reference_c=s.get("reference_c") if isinstance(s.get("reference_c"), (int, float)) else None,
        k=k, d=_value(p, "baseline_c_per_h") or 0.0, heating=heating,
        heating_basis=s.get("heating") if heating is not None else None,
        lag_min=float(dyn.get("lag_min") or 0.0), tau_min=float(dyn.get("tau_min") or 0.0),
        solar=_value(p, "solar_c_per_h") if s.get("solar") else None,
        night=_value(p, "night_c_per_h") if s.get("night") else None,
        coupling=coupling, uncertainty=unc, confidence=(doc.get("confidence") or {}).get("level") or "low",
        fingerprint_hash=doc.get("fingerprint_hash"),
    )


def load_model(doc: Any, *, fingerprint_hash: str | None = None, allow_preliminary: bool = False
               ) -> ThermalModel | Unavailable:
    """A validated model ready to evaluate, or Unavailable (unreadable, built for another configuration,
    unidentifiable, preliminary)."""
    if doc is None:
        return Unavailable("no_model", "No thermal model has been learned for this room yet")
    try:
        model = from_document(doc)
    except ModelError as err:
        return Unavailable(err.code, str(err))
    if fingerprint_hash is not None and model.fingerprint_hash != fingerprint_hash:
        return Unavailable("invalidated", "The room's climate configuration changed after this model was learned; "
                           "it must be rebuilt")
    if model.status == "unidentifiable":
        return Unavailable("unidentifiable", "No reliable model could be identified from the observations")
    if model.preliminary and not allow_preliminary:
        return Unavailable("preliminary", "The model is preliminary: the room has less usable history than the "
                           "configured minimum")
    warnings = []
    if model.heating_basis == "inferred":
        warnings.append("The heating response is inferred from setpoints, not from reported heating activity")
    if model.preliminary:
        warnings.append("Preliminary model (less usable history than the configured minimum)")
    if warnings:
        model = ThermalModel(**{**model.__dict__, "warnings": tuple(warnings)})
    return model


# ------------------------------------------------------------- the physics
def rates(model: ThermalModel, inp: Inputs, u_f: float) -> tuple[float, float, list[str]]:
    """(A, B, missing) with dT/dt = A − B·T for constant inputs. ``missing`` lists inputs the model needs but
    did not get (their terms are then left out and the caller is warned)."""
    missing: list[str] = []
    if model.reference == "outdoor":
        if inp.outdoor_c is None:
            missing.append("outdoor_c")
            ref = None
        else:
            ref = inp.outdoor_c
    else:
        ref = model.reference_c
    B = model.k if ref is not None else 0.0
    A = model.d + (model.k * ref if ref is not None else 0.0)
    if model.heating is not None:
        A += model.heating * u_f
    if model.solar is not None:
        if inp.solar is None:
            missing.append("solar")
        else:
            A += model.solar * inp.solar
    if model.night is not None:
        if inp.night is None:
            missing.append("night")
        elif inp.night:
            A += model.night
    for area, c in model.coupling.items():
        value = (inp.neighbours or {}).get(area)
        if value is None:
            missing.append(f"neighbour:{area}")
            continue
        A += c * value
        B += c
    return A, B, missing


def emitter_mean(u: float, u_f: float, tau_min: float, dt_h: float) -> float:
    """The emitter's mean output over a step whose (delayed) input is ``u``, starting from state ``u_f``: the
    exact average of the first-order response. Heating reported during a step acts within that step."""
    if tau_min <= 0:
        return u
    r = dt_h * 60.0 / tau_min
    return u + (u_f - u) * (1.0 - math.exp(-r)) / r


def step(model: ThermalModel, temp_c: float, u_f: float, inp: Inputs, dt_h: float) -> tuple[float, float, list[str]]:
    """One exact step of the linear model with the inputs held constant: (temperature, heating state, missing).
    ``inp.heating`` must already be delayed by the model's lag; ``u_f`` is the emitter's state at the start."""
    u = min(1.0, max(0.0, inp.heating or 0.0))
    A, B, missing = rates(model, inp, emitter_mean(u, u_f, model.tau_min, dt_h))
    if B > 1e-9:
        eq = A / B
        temp = eq + (temp_c - eq) * math.exp(-B * dt_h)
    else:
        temp = temp_c + A * dt_h
    if model.tau_min > 0:
        u_f = u + (u_f - u) * math.exp(-dt_h * 60.0 / model.tau_min)
    else:
        u_f = u
    return temp, u_f, missing


def _band(model: ThermalModel, minutes: float) -> tuple[float, bool]:
    """The 80 % half-width of the error after ``minutes`` (interpolated from the validated horizons)."""
    known = sorted((h, q.get("q80")) for h, q in model.uncertainty.items() if q.get("q80") is not None)
    if not known:
        return math.nan, True
    points = [(0.0, 0.0), *((float(h), float(v)) for h, v in known)]  # type: ignore[arg-type]
    if minutes <= points[-1][0]:
        for (h0, v0), (h1, v1) in zip(points, points[1:], strict=False):
            if h0 <= minutes <= h1:
                return v0 + (v1 - v0) * (minutes - h0) / max(h1 - h0, 1e-9), False
    h, v = points[-1]
    return v * math.sqrt(minutes / h), True


def simulate(model: ThermalModel, start_c: float, inputs: Sequence[Inputs], *, step_min: float = STEP_MIN,
             heating_history: Sequence[float] | None = None, heating_state: float | None = None
             ) -> Trajectory | Unavailable:
    """The room's temperature under the given per-step conditions (``inputs[i]`` holds from step i to i+1).

    ``heating_history``: the heating fraction of the steps *before* the start (newest last), for the model's
    delay; without it the first input's heating is assumed to have held. ``heating_state``: the emitter's
    smoothed heating at the start (default: the delayed heating at the start, i.e. steady).
    """
    if not math.isfinite(start_c):
        return Unavailable("invalid_input", "The start temperature must be a number", ("start_c",))
    if model.reference == "outdoor" and any(inp.outdoor_c is None for inp in inputs):
        # Leaving the loss out would let the baseline warm the room without limit: refuse instead.
        return Unavailable("missing_input", "This model needs the outdoor temperature for every step",
                           ("outdoor_c",))
    if not inputs:
        return Trajectory(start_c, step_min, (start_c,), (start_c,), (start_c,), model.warnings)
    lag = int(round(model.lag_min / step_min))
    first = inputs[0].heating or 0.0
    past = list(heating_history or [])
    seq = [inp.heating or 0.0 for inp in inputs]

    def delayed(i: int) -> float:
        j = i - lag
        if j >= 0:
            return seq[j]
        back = len(past) + j
        return past[back] if 0 <= back < len(past) else first

    u_f = heating_state if heating_state is not None else delayed(0)
    temp = start_c
    temps, low, high = [start_c], [start_c], [start_c]
    missing: set[str] = set()
    extrapolated = False
    dt_h = step_min / 60.0
    for i, inp in enumerate(inputs):
        temp, u_f, miss = step(model, temp, u_f, Inputs(inp.outdoor_c, delayed(i), inp.solar, inp.night,
                                                        inp.neighbours), dt_h)
        missing.update(miss)
        temps.append(temp)
        width, beyond = _band(model, (i + 1) * step_min)
        extrapolated = extrapolated or beyond
        low.append(temp - width)
        high.append(temp + width)
    warnings = list(model.warnings)
    if "outdoor_c" in missing:
        warnings.append("The outdoor temperature was not given for every step: the loss to outdoors was left out")
    for m in sorted(x for x in missing if x.startswith("neighbour:")):
        warnings.append(f"The temperature of {m.split(':', 1)[1]} was not given: its influence was left out")
    if "solar" in missing:
        warnings.append("The direct-sun index was not given: the solar term was left out")
    if "night" in missing:
        warnings.append("Day or night was not given: the night baseline was left out")
    if extrapolated:
        warnings.append(f"Beyond {max(model.uncertainty or {0: {}})} minutes the uncertainty band is extrapolated, "
                        "not validated")
    if any(math.isnan(x) for x in low):
        warnings.append("No validated uncertainty is available for this model")
    return Trajectory(start_c, step_min, tuple(temps), tuple(low), tuple(high), tuple(dict.fromkeys(warnings)))


def estimate_change(model: ThermalModel, start_c: float, hours: float, *, outdoor_c: float | None = None,
                    heating: float = 0.0, solar: float | None = None, night: bool | None = None,
                    neighbours: dict[str, float] | None = None) -> dict[str, Any] | Unavailable:
    """The temperature change over ``hours`` under constant conditions (heating fraction 0..1 held throughout
    and before the start)."""
    if not 0 < hours <= 48:
        return Unavailable("invalid_input", "The interval must be between 0 and 48 hours", ("hours",))
    if model.heating is None and heating:
        return Unavailable("no_heating_response", "This model has no heating response (it could not be learned)",
                           ("heating_response",))
    n = max(1, int(round(hours * 60 / STEP_MIN)))
    inp = Inputs(outdoor_c, heating, solar if solar is not None else (0.0 if model.solar is not None else None),
                 night, neighbours)
    traj = simulate(model, start_c, [inp] * n, heating_history=[heating] * 64, heating_state=heating)
    if isinstance(traj, Unavailable):
        return traj
    return {"available": True, "kind": "model_estimate", "start_c": start_c, "hours": hours,
            "end_c": round(traj.temps[-1], 3), "change_c": round(traj.temps[-1] - start_c, 3),
            "band_c": [round(traj.low[-1], 3), round(traj.high[-1], 3)], "warnings": list(traj.warnings)}


def equilibrium(model: ThermalModel, inp: Inputs, heating: float) -> float | None:
    """Where the room settles under constant conditions (None when it has no restoring force)."""
    A, B, _missing = rates(model, inp, heating)
    return A / B if B > 1e-9 else None


def evaluate(model: ThermalModel, start_c: float, *, outdoor_c: float | None, heating: float, hours: float = 6.0,
             solar: float | None = None, night: bool | None = None,
             neighbours: dict[str, float] | None = None) -> dict[str, Any] | Unavailable:
    """A room under an assumed outdoor temperature and heating fraction: the trajectory, where it would settle
    with and without its heating, and the current rate of change. For A7.2's comparisons; nothing is
    commanded."""
    change = estimate_change(model, start_c, hours, outdoor_c=outdoor_c, heating=heating, solar=solar, night=night,
                             neighbours=neighbours)
    if isinstance(change, Unavailable):
        return change
    inp = Inputs(outdoor_c, heating, solar if solar is not None else (0.0 if model.solar is not None else None),
                 night, neighbours)
    A, B, _m = rates(model, inp, heating)
    return {**change, "rate_now_c_per_h": round(A - B * start_c, 3),
            "settles_heating_c": _r(equilibrium(model, inp, 1.0)) if model.heating is not None else None,
            "settles_unheated_c": _r(equilibrium(model, inp, 0.0)),
            "time_constant_h": _r(model.time_constant_h), "confidence": model.confidence}


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


@dataclass
class Param:
    """A fitted parameter as stored (see the module docstring)."""

    value: float | None
    unit: str
    available: bool
    se: float | None = None
    confidence: str = "none"
    basis: str | None = None
    reason: str | None = None
    text: str | None = None
    bound: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {"value": None if self.value is None else round(self.value, 5), "unit": self.unit,
               "available": self.available, "se": None if self.se is None else round(self.se, 5),
               "confidence": self.confidence if self.available else "none", "basis": self.basis,
               "reason": self.reason, "text": self.text}
        if self.bound:
            out["bound"] = self.bound
        out.update(self.extra)
        return out
