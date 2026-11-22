"""Climate thermal learning (Phase A7.1): fitting, model selection, validation and confidence.

Pure code: a ``RoomGrid`` (``climate_prep``) in, a model document (``climate_thermal_model``) or a diagnostic
out. No dependency beyond the standard library (the App image has no numerical libraries, and the problem is a
small linear one).

Method
------
1. **Blocks.** Usable runs of the grid (one temperature source, window closed, heating state known) are cut into
   non-overlapping 30-minute blocks. The target is the block's temperature change rate ``(T_end − T_start) /
   0.5 h``; the regressors are the block means of the model's terms (``climate_thermal_model``). 30 minutes,
   not 5: a 0.1 °C sensor step over 5 minutes would read as 1.2 °C/h.
2. **Chronological split.** The usable time is split by time: the first 75 % trains, the last 25 % is held out
   and used **only** for the final accuracy report. Model selection uses a second split *inside* the training
   part (its last 30 %). Nothing after the split point is used for fitting or selection.
3. **Constrained least squares.** Coefficients that must be ≥ 0 are fitted by an exhaustive active-set search
   (the problem has at most a handful of columns); an estimate outside ``BOUNDS`` is rejected.
4. **Forward selection.** The base model is the loss to the reference (outdoor, or fixed without an outdoor
   source) plus a baseline. Heating (with its delay and emitter time constant chosen on the training blocks),
   direct sun, a night baseline and each zone neighbour are then tried in turn; a term is kept only when it
   improves the held-out (inner) simulation error by at least 3 % **and** its coefficient is positive and
   significant. More inputs never mean a more complex model by themselves.
5. **Validation.** From origins every 30 minutes in the held-out period, the model is *simulated* 1 h and 3 h
   ahead with the observed conditions (never with observed temperatures), through the same ``step`` A7.2 uses,
   and compared with the measured temperature. Baselines: persistence (the temperature stays) and the passive
   model (loss and baseline only, fitted on confirmed non-heating blocks).
6. **Confidence** combines usable history, episode counts, sensor quality, parameter identifiability (standard
   errors inflated for autocorrelated residuals), stability across forward-chaining training windows, and the
   held-out skill over persistence. The uncertainty band A7.2 receives is the empirical error distribution of the
   held-out simulations, not a formula.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import itertools
import math
import statistics
from typing import Any

from . import thermal_model as tm
from .prep import GRID_S, REASONS, Episode, RoomGrid, episodes, exclusions, heating_of, runs, secondary_hours

BLOCK_STEPS = 6
BLOCK_H = BLOCK_STEPS * GRID_S / 3600.0
LAGS_MIN = (0, 5, 10, 15, 20, 30, 45, 60)
TAUS_MIN = (0, 10, 20, 30, 45, 60, 90)
HORIZONS = {60: 12, 180: 36}
#: Held-out validation also reports 6 hours ahead (not used to choose the model's terms): the App publishes
#: predictions and warm-up times beyond 3 hours, and their accuracy must be measured, not assumed.
VALIDATE_HORIZONS = {60: 12, 180: 36, 360: 72}
ORIGIN_EVERY = 6
TRAIN_FRACTION = 0.75
INNER_FRACTION = 0.7
IMPROVEMENT = 0.03
MIN_USABLE_HOURS = 24.0
MIN_BLOCKS = 24
MIN_TEST_ORIGINS = 8
#: The outdoor temperature is the model's reference only when it is known for this share of the training time.
OUTDOOR_MIN_SHARE = 0.6
#: Rolling-origin folds inside the training part for term selection (fractions of its usable time).
FOLDS = (0.55, 0.70, 0.85, 1.0)
MAX_NEIGHBOURS = 3
#: Episodes needed before a parameter's confidence may be moderate / high.
EPISODES_FOR = {"moderate": 5, "high": 15}
PASSIVE_HOURS_FOR = {"moderate": 24.0, "high": 72.0}
NONNEG = {"ref", "heat", "solar"}  # coupling columns too (prefix cpl:)


# ------------------------------------------------------------ linear algebra
def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _invert(a: list[list[float]]) -> list[list[float]] | None:
    n = len(a)
    cols = []
    for i in range(n):
        e = [1.0 if j == i else 0.0 for j in range(n)]
        x = _solve(a, e)
        if x is None:
            return None
        cols.append(x)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


@dataclass
class Design:
    names: list[str]
    rows: list[list[float]]
    y: list[float]
    t: list[float]      # each block's start time

    def __len__(self) -> int:
        return len(self.y)

    def subset(self, keep: Callable[[float], bool]) -> Design:
        idx = [i for i, t in enumerate(self.t) if keep(t)]
        return Design(self.names, [self.rows[i] for i in idx], [self.y[i] for i in idx], [self.t[i] for i in idx])


@dataclass
class Fit:
    names: list[str]
    coef: dict[str, float]
    se: dict[str, float]
    sse: float
    n: int
    rho: float
    at_zero: set[str] = field(default_factory=set)


def nonneg(name: str) -> bool:
    return name in NONNEG or name.startswith("cpl:")


def least_squares(design: Design) -> Fit | None:
    """Least squares with the non-negativity constraints, by exhaustive active sets (few columns)."""
    names = design.names
    p = len(names)
    n = len(design)
    if n <= p + 2:
        return None
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    yty = 0.0
    for row, y in zip(design.rows, design.y, strict=True):
        for i in range(p):
            ri = row[i]
            if ri:
                xty[i] += ri * y
                xi = xtx[i]
                for j in range(i, p):
                    xi[j] += ri * row[j]
        yty += y * y
    for i in range(p):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
    ridge = 1e-9 * (sum(xtx[i][i] for i in range(p)) / p or 1.0)
    constrained = [i for i, name in enumerate(names) if nonneg(name)]
    best: tuple[float, list[float], set[int]] | None = None
    for size in range(len(constrained) + 1):
        for zero in itertools.combinations(constrained, size):
            free = [i for i in range(p) if i not in zero]
            a = [[xtx[i][j] + (ridge if i == j else 0.0) for j in free] for i in free]
            beta = _solve(a, [xty[i] for i in free])
            if beta is None:
                continue
            full = [0.0] * p
            for i, v in zip(free, beta, strict=True):
                full[i] = v
            if any(full[i] < -1e-12 for i in constrained if i not in zero):
                continue
            sse = yty - 2 * sum(full[i] * xty[i] for i in range(p)) + sum(
                full[i] * xtx[i][j] * full[j] for i in range(p) for j in range(p))
            if best is None or sse < best[0] - 1e-12:
                best = (max(sse, 0.0), full, set(zero))
        if best is not None and size == 0:
            break  # the unconstrained optimum is feasible: it is the optimum
    if best is None:
        return None
    sse, coef, zero = best
    free = [i for i in range(p) if i not in zero]
    residuals = [y - sum(c * x for c, x in zip(coef, row, strict=True)) for row, y in zip(design.rows, design.y,
                                                                                        strict=True)]
    rho = _lag1(residuals, design.t)
    inflate = math.sqrt((1 + rho) / (1 - rho))
    sigma2 = sse / max(1, n - len(free))
    inv = _invert([[xtx[i][j] + (ridge if i == j else 0.0) for j in free] for i in free])
    se = {}
    for k, i in enumerate(free):
        se[names[i]] = math.sqrt(max(0.0, sigma2 * inv[k][k])) * inflate if inv else math.inf
    for i in zero:
        se[names[i]] = math.inf
    return Fit(names, dict(zip(names, coef, strict=True)), se, sse, n, rho, {names[i] for i in zero})


def _lag1(residuals: list[float], times: list[float]) -> float:
    """Lag-1 autocorrelation of consecutive blocks' residuals (only blocks that follow each other)."""
    pairs = [(residuals[i - 1], residuals[i]) for i in range(1, len(residuals))
             if abs(times[i] - times[i - 1] - BLOCK_H * 3600) < 1]
    if len(pairs) < 10:
        return 0.0
    mean = statistics.fmean(residuals)
    num = sum((a - mean) * (b - mean) for a, b in pairs)
    den = sum((r - mean) ** 2 for r in residuals) * len(pairs) / len(residuals)
    return min(0.9, max(0.0, num / den)) if den > 0 else 0.0


# ------------------------------------------------------------------ inputs
@dataclass
class Series:
    """The step-level inputs the fit and the simulations read."""

    grid: RoomGrid
    usable: list[bool]
    u: list[float | None]
    ref: list[float | None]
    reference: str
    reference_c: float | None
    filtered: dict[tuple[int, int], list[float | None]] = field(default_factory=dict)
    outdoor_share: float = 0.0

    def heat(self, lag: int, tau: int) -> list[float | None]:
        key = (lag, tau)
        if key not in self.filtered:
            self.filtered[key] = filtered_heating(self.u, lag, tau)
        return self.filtered[key]


def filtered_heating(u: Sequence[float | None], lag_min: int, tau_min: int) -> list[float | None]:
    """The emitter's mean heating output over each grid step: the heating fraction delayed by ``lag`` and smoothed
    with time constant ``tau`` (exactly as ``climate_thermal_model.step`` applies it, so the fit and every
    simulation agree; heating reported during a step acts within that step). Unknown until the filter has run
    for two time constants on known values."""
    lag = int(round(lag_min * 60 / GRID_S))
    dt_h = GRID_S / 3600.0
    decay = math.exp(-GRID_S / 60.0 / tau_min) if tau_min > 0 else 0.0
    warm_needed = int(math.ceil(2 * tau_min * 60 / GRID_S)) if tau_min > 0 else 0
    out: list[float | None] = []
    state: float | None = None
    warm = 0
    for i in range(len(u)):
        j = i - lag
        x = u[j] if j >= 0 else None
        if x is None:
            state, warm = None, 0
            out.append(None)
            continue
        if state is None:
            state, warm = x, 0
        out.append(tm.emitter_mean(x, state, tau_min, dt_h) if warm >= warm_needed else None)
        state = x + (state - x) * decay
        warm += 1
    return out


def emitter_state(u: Sequence[float | None], lag_min: int, tau_min: int, i: int) -> float | None:
    """The emitter's state at the *start* of step i (what a simulation starting at i begins with)."""
    lag = int(round(lag_min * 60 / GRID_S))
    if tau_min <= 0:
        j = i - lag
        return u[j] if j >= 0 else None
    decay = math.exp(-GRID_S / 60.0 / tau_min)
    back = int(math.ceil(4 * tau_min * 60 / GRID_S))
    state: float | None = None
    for k in range(max(0, i - back), i):
        j = k - lag
        x = u[j] if j >= 0 else None
        if x is None:
            return None
        state = x if state is None else x + (state - x) * decay
    return state


def make_series(grid: RoomGrid, *, until: float | None = None) -> Series:
    """The fit's inputs. The reference is the outdoor temperature when it is known for at least
    ``OUTDOOR_MIN_SHARE`` of the usable time before ``until`` (the training part); otherwise the simpler fixed
    reference: the median room temperature of that same part (nothing after ``until`` is read)."""
    u = heating_of(grid)
    usable = [w == "usable" for w in grid.why]
    train = [i for i, ok in enumerate(usable) if ok and (until is None or grid.times[i] < until)]
    known = sum(1 for i in train if grid.outdoor[i] is not None)
    share = known / len(train) if train else 0.0
    if train and share >= OUTDOOR_MIN_SHARE:
        s = Series(grid, usable, u, grid.outdoor, "outdoor", None)
    else:
        temps = sorted(grid.temp[i] for i in train if grid.temp[i] is not None)  # type: ignore[misc]
        ref_c = round(temps[len(temps) // 2], 2) if temps else 20.0
        s = Series(grid, usable, u, [ref_c] * len(grid.times), "fixed", ref_c)
    s.outdoor_share = round(share, 3)
    return s


@dataclass
class Structure:
    heating: bool = False
    lag: int = 0
    tau: int = 0
    solar: bool = False
    night: bool = False
    coupling: tuple[str, ...] = ()

    def columns(self) -> list[str]:
        cols = ["ref", "const"]
        if self.heating:
            cols.append("heat")
        if self.solar:
            cols.append("solar")
        if self.night:
            cols.append("night")
        cols += [f"cpl:{a}" for a in self.coupling]
        return cols

    def with_(self, **changes: Any) -> Structure:
        return Structure(**{**self.__dict__, **changes})


def design(s: Series, st: Structure, *, keep: Callable[[int], bool] | None = None,
           where: Callable[[int], bool] | None = None) -> Design:
    """The blocks of ``s`` for structure ``st`` (only blocks where every column's input is known)."""
    g = s.grid
    cols = st.columns()
    heat = s.heat(st.lag, st.tau) if st.heating else None
    rows, ys, ts = [], [], []
    n = len(g.times)
    i = 0
    while i + BLOCK_STEPS < n:
        span = range(i, i + BLOCK_STEPS + 1)
        if not all(s.usable[j] for j in span) or len({g.source[j] for j in span}) != 1:
            i += 1
            continue
        if (keep is not None and not keep(i)) or (where is not None and not where(i)):
            i += BLOCK_STEPS
            continue
        steps = range(i, i + BLOCK_STEPS)
        tbar = [(g.temp[j] + g.temp[j + 1]) / 2 for j in steps]  # type: ignore[operator]
        row: list[float] = []
        ok = True
        for c in cols:
            if c == "ref":
                vals = [s.ref[j] for j in steps]
                if any(v is None for v in vals):
                    ok = False
                    break
                row.append(statistics.fmean(v - tb for v, tb in zip(vals, tbar, strict=True)))  # type: ignore[operator]
            elif c == "const":
                row.append(1.0)
            elif c == "heat":
                vals = [heat[j] for j in steps]  # type: ignore[index]
                if any(v is None for v in vals):
                    ok = False
                    break
                row.append(statistics.fmean(vals))  # type: ignore[arg-type]
            elif c == "solar":
                vals = [g.solar[j] for j in steps] if g.solar is not None else [None]
                if any(v is None for v in vals):
                    ok = False
                    break
                row.append(statistics.fmean(vals))  # type: ignore[arg-type]
            elif c == "night":
                row.append(statistics.fmean(g.night[j] for j in steps))
            else:
                other = g.neighbours.get(c[4:])
                vals = [other[j] for j in steps] if other else [None]
                if any(v is None for v in vals):
                    ok = False
                    break
                row.append(statistics.fmean(v - tb for v, tb in zip(vals, tbar, strict=True)))  # type: ignore[operator]
        if ok:
            rows.append(row)
            ys.append((g.temp[i + BLOCK_STEPS] - g.temp[i]) / BLOCK_H)  # type: ignore[operator]
            ts.append(g.times[i])
        i += BLOCK_STEPS
    return Design(cols, rows, ys, ts)


# ------------------------------------------------------------- simulation
def model_of(s: Series, st: Structure, fit: Fit, *, heating: bool = True) -> tm.ThermalModel:
    c = fit.coef
    return tm.ThermalModel(
        area_id=s.grid.area_id, trained_at="", status="low", preliminary=False, reference=s.reference,
        reference_c=s.reference_c, k=c.get("ref", 0.0), d=c.get("const", 0.0),
        heating=c.get("heat") if st.heating and heating else None,
        heating_basis="reported" if st.heating and heating else None,
        lag_min=float(st.lag), tau_min=float(st.tau), solar=c.get("solar") if st.solar else None,
        night=c.get("night") if st.night else None,
        coupling={a: c[f"cpl:{a}"] for a in st.coupling}, uncertainty={}, confidence="low", fingerprint_hash=None)


def origins(s: Series, st: Structure, *, lo: float, hi: float, horizon: int) -> list[int]:
    """Simulation start steps in [lo, hi): every 30 minutes, where the whole horizon is usable with one source
    and every input the structure needs is known."""
    g = s.grid
    heat = s.heat(st.lag, st.tau) if st.heating else None
    lag = int(round(st.lag * 60 / GRID_S))
    out = []
    n = len(g.times)
    for o in range(0, n - horizon, ORIGIN_EVERY):
        # The whole horizon lies in [lo, hi): a selection score never reads a temperature after its split.
        if not lo <= g.times[o] or not g.times[o + horizon] < hi:
            continue
        span = range(o, o + horizon + 1)
        if not all(s.usable[j] for j in span) or len({g.source[j] for j in span}) != 1:
            continue
        steps = range(o, o + horizon)
        if any(s.ref[j] is None for j in steps):
            continue
        if heat is not None and (heat[o] is None or any(s.u[j - lag] is None for j in steps if j - lag >= 0)):
            continue
        if st.solar and (g.solar is None or any(g.solar[j] is None for j in steps)):
            continue
        if any(g.neighbours.get(a) is None or any(g.neighbours[a][j] is None for j in steps) for a in st.coupling):
            continue
        out.append(o)
    return out


def run(s: Series, model: tm.ThermalModel, st: Structure, o: int, horizon: int, *,
        hold_neighbours: bool = False) -> list[float]:
    """The simulated temperatures at steps o+1 .. o+horizon (observed conditions, never observed temperatures).
    The heating delay and emitter are the model's own. ``hold_neighbours``: the zone's other rooms stay at their
    temperature at the start (what a prediction will know), instead of their measured course."""
    g = s.grid
    lag_min, tau_min = int(model.lag_min), int(model.tau_min)
    lag = int(round(lag_min * 60 / GRID_S))
    heated = model.heating is not None
    u_f = (emitter_state(s.u, lag_min, tau_min, o) or 0.0) if heated else 0.0
    temp = g.temp[o]
    out = []
    dt_h = GRID_S / 3600.0
    for j in range(o, o + horizon):
        delayed = s.u[j - lag] if heated and j - lag >= 0 else 0.0
        k = o if hold_neighbours else j
        inp = tm.Inputs(outdoor_c=s.ref[j] if s.reference == "outdoor" else None, heating=delayed or 0.0,
                        solar=g.solar[j] if model.solar is not None and g.solar is not None else None,
                        night=bool(g.night[j]) if model.night is not None else None,
                        neighbours={a: g.neighbours[a][k] for a in model.coupling} if model.coupling else None)
        temp, u_f, _missing = tm.step(model, temp, u_f, inp, dt_h)  # type: ignore[arg-type]
        out.append(temp)
    return out


def score(s: Series, st: Structure, models: Sequence[tm.ThermalModel], *, lo: float, hi: float
          ) -> list[float] | None:
    """Each model's mean held-out RMSE over the 1 h and 3 h horizons, on the same origins (those where ``st``'s
    inputs are known, the largest structure being compared)."""
    totals = []
    for model in models:
        parts = []
        for minutes, horizon in HORIZONS.items():
            starts = origins(s, st, lo=lo, hi=hi, horizon=horizon)
            if len(starts) < MIN_TEST_ORIGINS and minutes > 60:
                continue
            if len(starts) < MIN_TEST_ORIGINS:
                return None
            errs = [run(s, model, st, o, horizon)[-1] - s.grid.temp[o + horizon] for o in starts]  # type: ignore[operator]
            parts.append(math.sqrt(statistics.fmean(e * e for e in errs)))
        if not parts:
            return None
        totals.append(statistics.fmean(parts))
    return totals


# ---------------------------------------------------------------- learning
@dataclass
class Learned:
    """The result of ``learn``: a model document, or a diagnostic when no model could be fitted."""

    doc: dict[str, Any] | None
    diagnostic: dict[str, Any]


def _split_time(s: Series, fraction: float, lo: float, hi: float) -> float | None:
    times = [t for t, ok in zip(s.grid.times, s.usable, strict=True) if ok and lo <= t < hi]
    if len(times) < 2:
        return None
    return times[min(len(times) - 1, int(fraction * len(times)))]


def choose_dynamics(s: Series, st: Structure, keep: Callable[[int], bool]) -> tuple[int, int, dict[str, Any]]:
    """The heating delay and emitter time constant with the least training error (blocks only), and whether they
    are identifiable (the best clearly beats "no delay, no smoothing")."""
    profile: dict[tuple[int, int], float] = {}
    for lag in LAGS_MIN:
        for tau in TAUS_MIN:
            # Compare on the blocks every candidate can use (the longest warm-up and delay).
            d = design(s, st.with_(heating=True, lag=lag, tau=tau),
                       keep=lambda i: keep(i) and _all_known(s, i, max(LAGS_MIN), max(TAUS_MIN)))
            fit = least_squares(d)
            if fit is not None and fit.coef.get("heat", 0) > 0:
                profile[(lag, tau)] = fit.sse / max(1, fit.n)
    if not profile:
        return 0, 0, {"identifiable": False, "reason": "No heating blocks with enough history to compare delays"}
    best = min(profile, key=profile.__getitem__)
    base = profile.get((0, 0))
    gain = (base - profile[best]) / base if base else 0.0
    identifiable = gain >= 0.02 and best != (0, 0)
    return best[0], best[1], {"identifiable": identifiable, "gain": round(gain, 4),
                              "reason": None if identifiable else
                              "Delay and residual warming are within the noise of the measurements (the room reacts "
                              "within the 5-minute grid, or the data cannot separate them)"}


def _all_known(s: Series, i: int, lag_min: int, tau_min: int) -> bool:
    lag = int(round(lag_min * 60 / GRID_S))
    warm = int(math.ceil(2 * tau_min * 60 / GRID_S))
    lo = i - lag - warm
    return lo >= 0 and all(s.u[j] is not None for j in range(lo, i + BLOCK_STEPS))


def _plausible(fit: Fit) -> str | None:
    """Why a fit is not physically plausible (None when it is)."""
    for name, value in fit.coef.items():
        bound = tm.BOUNDS[_bound_name(name)]
        if not bound[0] - 1e-9 <= value <= bound[1] + 1e-9 or (value >= bound[1] - 1e-9 and bound[1] > 0):
            return f"{name} = {value:.3f} is outside its plausible range {bound[0]}–{bound[1]}"
    return None


def _bound_name(name: str) -> str:
    return {"ref": "k_per_h", "const": "baseline_c_per_h", "heat": "heating_c_per_h", "solar": "solar_c_per_h",
            "night": "night_c_per_h"}.get(name, "coupling_per_h")


def learn(grid: RoomGrid, *, min_history_days: int, now: float | None = None) -> Learned:
    """Fit, select and validate a room's thermal model from its prepared grid."""
    now = now if now is not None else datetime.now(UTC).timestamp()
    probe = make_series(grid)  # usable steps only (no values): for the split
    eps = episodes(grid)
    usable_h = sum(probe.usable) * GRID_S / 3600.0
    lo, hi = (grid.times[0], grid.times[-1] + GRID_S) if grid.times else (0.0, 0.0)
    split = _split_time(probe, TRAIN_FRACTION, lo, hi) if usable_h >= MIN_USABLE_HOURS else None
    s = make_series(grid, until=split)
    evidence = _evidence(grid, s, eps)
    diag: dict[str, Any] = {"evidence": evidence, "reference": s.reference}
    if usable_h < MIN_USABLE_HOURS:
        diag["status"] = "collecting" if usable_h < 1 else "insufficient"
        diag["reason"] = f"{usable_h:.1f} usable hours so far; fitting starts at {MIN_USABLE_HOURS:.0f} hours"
        return Learned(None, diag)
    cuts = [_split_time(s, f, lo, split) if split else None for f in FOLDS[:-1]]
    if split is None or any(c is None for c in cuts):
        diag.update(status="insufficient", reason="Too little usable time to keep a held-out period")
        return Learned(None, diag)
    cuts = [*cuts, split]
    times = grid.times

    def before(t: float) -> Callable[[int], bool]:
        return lambda i: times[i] + BLOCK_STEPS * GRID_S < t  # the whole block ends before t

    in_train = before(split)
    folds = [(before(cuts[k]), cuts[k], cuts[k + 1]) for k in range(len(cuts) - 1)]
    selection: list[dict[str, Any]] = []

    base = Structure()
    if least_squares(design(s, base, keep=folds[0][0])) is None or len(design(s, base, keep=in_train)) < MIN_BLOCKS:
        diag.update(status="insufficient", reason="Not enough usable 30-minute blocks to fit a model")
        return Learned(None, diag)
    current = base
    dynamics: dict[str, Any] = {"identifiable": False, "reason": "No heating term"}
    heating_reason: str | None = None
    candidates: list[tuple[str, Callable[[Structure], Structure]]] = []
    if grid.heat_basis != "none" and any(u for u in s.u if u):
        candidates.append(("heating", lambda st: st.with_(heating=True)))
    elif grid.heat_basis == "none":
        heating_reason = "No heating device is assigned to this room"
    else:
        heating_reason = "No confirmed heating was observed" if grid.heat_basis != "inferred" else \
            "No heating demand could be inferred"
    if grid.solar is not None:
        candidates.append(("solar", lambda st: st.with_(solar=True)))
    candidates.append(("night", lambda st: st.with_(night=True)))
    for area in list(grid.neighbours)[:MAX_NEIGHBOURS]:
        candidates.append((f"cpl:{area}", lambda st, a=area: st.with_(coupling=(*st.coupling, a))))
    term_reasons: dict[str, str] = {}

    for name, extend in candidates:
        cand = extend(current)
        if cand.heating:
            # Re-chosen with every candidate (a slow emitter must not absorb, say, the sun), on the earliest fold's
            # training blocks only: before every selection window.
            lag, tau, dynamics = choose_dynamics(s, cand, folds[0][0])
            cand = cand.with_(lag=lag, tau=tau)
        col = "heat" if name == "heating" else name
        fit = least_squares(design(s, cand, keep=in_train))
        if fit is None:
            term_reasons[name] = "Not enough blocks where this input is known"
            selection.append({"term": name, "accepted": False, "reason": term_reasons[name]})
            continue
        implausible = _plausible(fit)
        value, se = fit.coef.get(col, 0.0), fit.se.get(col, math.inf)
        if implausible:
            term_reasons[name] = f"The fit was not physically plausible ({implausible})"
        elif nonneg(col) and value <= 0:
            term_reasons[name] = "The fitted effect is zero"
        elif not math.isfinite(se) or abs(value) < 2 * se:
            term_reasons[name] = f"The effect is not distinguishable from noise ({value:.3f} ± {se:.3f})"
        if name in term_reasons:
            selection.append({"term": name, "accepted": False, "reason": term_reasons[name]})
            continue
        result = fold_gain(s, current, cand, folds)
        if result is None:
            term_reasons[name] = "Too few held-out periods where this input is known to test it"
            selection.append({"term": name, "accepted": False, "reason": term_reasons[name]})
            continue
        gain, positive, n, without, with_ = result
        t_value = abs(value) / se if se > 0 else math.inf
        # A clear gain, or a strongly determined effect that helps in most folds (one cloudy window must not
        # reject a real term).
        accepted = gain >= IMPROVEMENT or (gain >= 0 and t_value >= 5 and positive * 2 > n)
        selection.append({"term": name, "accepted": accepted, "gain": round(gain, 4), "folds": n,
                          "folds_improved": positive, "t": round(t_value, 1),
                          "rmse_without": round(without, 4), "rmse_with": round(with_, 4)})
        if accepted:
            current = cand
        else:
            term_reasons[name] = f"It did not improve the held-out accuracy enough ({gain * 100:+.1f} % over {n} " \
                                 f"periods, {IMPROVEMENT * 100:.0f} % needed)"

    # Backward pass: a term accepted early may be redundant once a later one explains the same variation.
    for name in [x["term"] for x in selection if x.get("accepted") and x["term"] != "heating"]:
        reduced = _without(current, name)
        if reduced.heating:
            lag, tau, _d = choose_dynamics(s, reduced, folds[0][0])
            reduced = reduced.with_(lag=lag, tau=tau)
        result = fold_gain(s, reduced, current, folds)
        if result is None:
            continue
        gain = result[0]
        if gain < IMPROVEMENT:
            current = reduced
            term_reasons[name] = f"Redundant once the other terms were included ({gain * 100:+.1f} %)"
            selection.append({"term": name, "accepted": False, "removed": True, "gain": round(gain, 4),
                              "reason": term_reasons[name]})

    # The selected structure, refitted on the whole training part (its dynamics chosen there too). A term whose
    # coefficient the constrained refit sets to zero is dropped (not stored as "available" with nothing in it).
    while True:
        if current.heating:
            lag, tau, dynamics = choose_dynamics(s, current, in_train)
            current = current.with_(lag=lag, tau=tau)
        final = least_squares(design(s, current, keep=in_train))
        zero = [c for c in (final.at_zero if final else set()) if c != "ref"]
        if not zero:
            break
        for c in zero:
            name = "heating" if c == "heat" else c
            term_reasons[name] = "The effect was zero once refitted on the whole training period"
            selection.append({"term": name, "accepted": False, "removed": True, "reason": term_reasons[name]})
            current = current.with_(heating=False, lag=0, tau=0) if c == "heat" else _without(current, c)
    no_loss = final is not None and final.coef.get("ref", 0) <= 1e-4
    if final is None or (problem := _plausible(final)) is not None or (no_loss and s.reference == "outdoor"):
        why = "the fit found no measurable heat loss (the observations do not vary enough)" if final is not None \
            and not _plausible(final) else (problem if final is not None else "the fit could not be solved")
        diag.update(status="unidentifiable", reason=f"No reliable model: {why}", selection=selection)
        return Learned(_unidentifiable_doc(grid, s, evidence, selection, diag["reason"], now, min_history_days), diag)

    # Baselines and the held-out validation.
    stability = _stability(s, current, final, in_train)  # may raise final.se (before the parameters are built)
    model = model_of(s, current, final)
    passive_fit = least_squares(design(s, Structure(), keep=in_train, where=lambda i: _passive_block(s, i)))
    passive = model_of(s, Structure(), passive_fit) if passive_fit is not None and passive_fit.coef.get("ref", 0) > 0 \
        and _plausible(passive_fit) is None else None
    validation = validate(s, current, model, passive, lo=split, hi=hi)
    doc = _document(grid, s, current, final, model, validation, stability, dynamics, evidence, eps, selection,
                    term_reasons, heating_reason, now=now, min_history_days=min_history_days, split=split,
                    passive=passive)
    diag.update(status=doc["status"], selection=selection)
    return Learned(doc, diag)


def fold_gain(s: Series, without: Structure, with_: Structure,
              folds: list[tuple[Callable[[int], bool], float, float]]) -> tuple[float, int, int, float, float] | None:
    """Rolling-origin comparison inside the training part: for each fold, both structures are fitted on the
    blocks before its window and simulated on the window (origins where ``with_``'s inputs are known).
    (pooled relative gain, folds improved, folds scored, pooled RMSE without, with)."""
    tot_without = tot_with = 0.0
    scored = positive = 0
    for keep, lo, hi in folds:
        fw = least_squares(design(s, without, keep=keep))
        fc = least_squares(design(s, with_, keep=keep))
        if fw is None or fc is None or _plausible(fc) is not None:
            continue
        scores = score(s, with_, [model_of(s, without, fw), model_of(s, with_, fc)], lo=lo, hi=hi)
        if scores is None:
            continue
        scored += 1
        positive += scores[1] < scores[0]
        tot_without += scores[0]
        tot_with += scores[1]
    if not scored or tot_without <= 0:
        return None
    return (tot_without - tot_with) / tot_without, positive, scored, tot_without / scored, tot_with / scored


def _without(st: Structure, name: str) -> Structure:
    if name == "solar":
        return st.with_(solar=False)
    if name == "night":
        return st.with_(night=False)
    return st.with_(coupling=tuple(a for a in st.coupling if f"cpl:{a}" != name))


def _passive_block(s: Series, i: int) -> bool:
    return all(s.grid.heat[j] == 0 for j in range(i, i + BLOCK_STEPS))


# -------------------------------------------------------------- validation
def validate(s: Series, st: Structure, model: tm.ThermalModel, passive: tm.ThermalModel | None, *, lo: float,
             hi: float) -> dict[str, Any]:
    """Held-out accuracy: the model, persistence and the passive baseline simulated from the same origins.

    The simulations use the conditions that were actually observed (outdoor temperature, reported heating, sun),
    i.e. a perfect forecast of them, on clean periods only (windows closed, heating known, one sensor). The other
    rooms of a thermal zone are held at their temperature at the start, as a prediction would know them."""
    g = s.grid
    out: dict[str, Any] = {"from": lo, "to": hi, "horizons": {}, "conditions": {},
                           "conditions_known": "observed", "neighbours": "held at the start" if st.coupling else None}
    test_steps = sum(1 for t, ok in zip(g.times, s.usable, strict=True) if ok and lo <= t < hi)
    span_h = max(0.0, (hi - lo) / 3600.0)
    out["usable_hours"] = round(test_steps * GRID_S / 3600.0, 2)
    out["coverage"] = round(out["usable_hours"] / span_h, 3) if span_h else 0.0
    errors_by: dict[int, list[float]] = {}
    for minutes, horizon in VALIDATE_HORIZONS.items():
        starts = origins(s, st, lo=lo, hi=hi, horizon=horizon)
        if len(starts) < MIN_TEST_ORIGINS:
            out["horizons"][str(minutes)] = {"origins": len(starts), "available": False,
                                             "reason": "Too few held-out periods of this length"}
            continue
        m_err, p_err, b_err = [], [], []
        cond: dict[str, list[tuple[float, float]]] = {"heating": [], "passive": [], "sunny": [], "night": []}
        for o in starts:
            actual = g.temp[o + horizon]
            sim = run(s, model, st, o, horizon, hold_neighbours=True)[-1]
            m_err.append(sim - actual)  # type: ignore[operator]
            p_err.append(g.temp[o] - actual)  # type: ignore[operator]
            if passive is not None:
                b_err.append(run(s, passive, Structure(), o, horizon)[-1] - actual)  # type: ignore[operator]
            window = range(o, o + horizon)
            pair = (abs(sim - actual), abs(g.temp[o] - actual))  # type: ignore[operator]
            heated = any((s.u[j] or 0) > 0 for j in window)
            cond["heating" if heated else "passive"].append(pair)
            if g.solar is not None and max((g.solar[j] or 0) for j in window) > 0.2:
                cond["sunny"].append(pair)
            if g.night[o]:
                cond["night"].append(pair)
        errors_by[minutes] = [abs(e) for e in m_err]
        out["horizons"][str(minutes)] = {
            "available": True, "origins": len(starts),
            "model": _metrics(m_err), "persistence": _metrics(p_err),
            "passive": _metrics(b_err) if b_err else None,
            "skill": round(1 - _mae(m_err) / _mae(p_err), 3) if _mae(p_err) > 0 else None,
        }
        out["conditions"][str(minutes)] = {
            k: {"origins": len(v), "model_mae": round(statistics.fmean(a for a, _b in v), 3),
                "persistence_mae": round(statistics.fmean(b for _a, b in v), 3)} for k, v in cond.items() if len(v) >= 3}
    out["uncertainty"] = {str(m): _quantiles(e) for m, e in errors_by.items() if len(e) >= MIN_TEST_ORIGINS}
    headline = "180" if out["horizons"].get("180", {}).get("available") else "60"
    out["headline"] = headline if out["horizons"].get(headline, {}).get("available") else None
    out["chart"] = _validation_chart(s, st, model, lo=lo, hi=hi)
    return out


def _mae(errs: list[float]) -> float:
    return statistics.fmean(abs(e) for e in errs) if errs else 0.0


def _metrics(errs: list[float]) -> dict[str, float]:
    return {"mae": round(_mae(errs), 3), "rmse": round(math.sqrt(statistics.fmean(e * e for e in errs)), 3),
            "bias": round(statistics.fmean(errs), 3), "n": len(errs)}


def _quantiles(abs_errors: list[float]) -> dict[str, float]:
    xs = sorted(abs_errors)

    def q(p: float) -> float:
        return round(xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))], 3)

    return {"q50": q(0.5), "q80": q(0.8), "q95": q(0.95), "n": len(xs)}


def _validation_chart(s: Series, st: Structure, model: tm.ThermalModel, *, lo: float, hi: float,
                      days: float = 7.0) -> dict[str, Any]:
    """The last ``days`` of the held-out period: measured temperature and the model's reconstruction, restarted
    from the measured temperature every 3 hours (each path is a 0–3 h simulation with observed conditions).
    Points every 15 minutes; a null breaks a line, so nothing is drawn across excluded or unobserved time."""
    g = s.grid
    start = max(lo, hi - days * 86400)
    every = 3
    horizon = HORIZONS[180]
    actual: list[list[Any]] = []
    model_pts: list[list[Any]] = []
    persistence: list[list[Any]] = []
    err_model: list[list[Any]] = []
    err_persist: list[list[Any]] = []
    excluded: list[list[float]] = []
    i = 0
    n = len(g.times)
    while i < n and g.times[i] < start:
        i += 1
    run_start: float | None = None
    for j in range(i, n):
        t = g.times[j]
        if not s.usable[j]:
            if run_start is None:
                run_start = t
        elif run_start is not None:
            excluded.append([round(run_start), round(t)])
            run_start = None
        if j % every == 0:
            actual.append([round(t), round(g.temp[j], 2) if s.usable[j] and g.temp[j] is not None else None])
    if run_start is not None:
        excluded.append([round(run_start), round(g.times[-1])])
    starts = [o for o in origins(s, st, lo=start, hi=hi, horizon=horizon) if (o - i) % horizon == 0] \
        or origins(s, st, lo=start, hi=hi, horizon=horizon)[::ORIGIN_EVERY]
    last_end = -1
    for o in starts:
        if o < last_end:
            continue
        path = run(s, model, st, o, horizon, hold_neighbours=True)
        model_pts.append([round(g.times[o]), round(g.temp[o], 2)])  # type: ignore[arg-type]
        persistence.append([round(g.times[o]), round(g.temp[o], 2)])  # type: ignore[arg-type]
        for k in range(every - 1, horizon, every):
            t = round(g.times[o + k + 1])
            actual_v = g.temp[o + k + 1]
            model_pts.append([t, round(path[k], 2)])
            persistence.append([t, round(g.temp[o], 2)])  # type: ignore[arg-type]
            err_model.append([t, round(path[k] - actual_v, 3)])  # type: ignore[operator]
            err_persist.append([t, round(g.temp[o] - actual_v, 3)])  # type: ignore[operator]
        end_t = round(g.times[o + horizon])
        for series in (model_pts, persistence, err_model, err_persist):
            series.append([end_t, None])
        last_end = o + horizon
    return {"from": round(start), "to": round(hi), "actual": actual, "model": model_pts, "persistence": persistence,
            "error_model": err_model, "error_persistence": err_persist, "excluded": excluded,
            "restart_hours": horizon * GRID_S / 3600.0}


def _stability(s: Series, st: Structure, final: Fit, in_train: Callable[[int], bool]) -> dict[str, Any]:
    """The same structure fitted on the two disjoint halves of the training blocks (earlier, later).

    ``cv``: how much the halves disagree (spread / mean). The halves' difference also bounds each parameter's
    standard error from below (``se ≥ |a₁ − a₂| / 2``, the error of a full-period estimate when the halves are
    independent): the regression's own standard errors assume the model is right, and are too small when it is
    not (an unmodelled cause leaves correlated residuals). ``final.se`` is raised in place accordingly."""
    d = design(s, st, keep=in_train)
    if len(d) < 2 * MIN_BLOCKS:
        return {"windows": 0, "cv": {}}
    mid = d.t[len(d.t) // 2]
    halves = [least_squares(d.subset(lambda t: t < mid)), least_squares(d.subset(lambda t: t >= mid))]
    fits = [f for f in halves if f is not None]
    cv: dict[str, float] = {}
    raised: list[str] = []
    for name in final.coef:
        values = [f.coef.get(name, 0.0) for f in fits]
        mean = statistics.fmean(abs(v) for v in values) if values else 0.0
        cv[name] = round(statistics.pstdev(values) / mean, 3) if mean > 1e-9 and len(values) > 1 else math.inf
        if len(values) == 2:
            floor = abs(values[0] - values[1]) / 2
            if floor > final.se.get(name, 0.0):
                final.se[name] = floor
                raised.append(name)
    return {"windows": len(fits), "cv": cv, "se_raised": raised,
            "values": {name: [round(f.coef.get(name, 0.0), 4) for f in fits] for name in final.coef}}


# --------------------------------------------------------------- evidence
def _evidence(grid: RoomGrid, s: Series, eps: list[Episode]) -> dict[str, Any]:
    step_h = GRID_S / 3600.0
    usable_h = sum(s.usable) * step_h
    observed = [w for w in grid.why if w != "not_observed"]
    by_kind: dict[str, list[Episode]] = {"heating": [], "passive": [], "solar": []}
    for e in eps:
        by_kind[e.kind].append(e)
    opened = runs(w == "open" for w in grid.window)
    open_h = sum(step_h for w in grid.window if w == "open")
    inferred_h = sum(step_h for h, hi, ok in zip(grid.heat, grid.heat_inf, s.usable, strict=True)
                     if ok and h is None and (hi or 0) > 0)
    first_obs = next((t for t, w in zip(grid.times, grid.why, strict=True) if w != "not_observed"), None)
    return {
        "calendar_days": round((grid.times[-1] - first_obs) / 86400.0, 2) if first_obs and grid.times else 0.0,
        "observed_hours": round(len(observed) * step_h, 2),
        "usable_hours": round(usable_h, 2), "usable_days": round(usable_h / 24.0, 2),
        "excluded": exclusions(grid),
        "excluded_text": {k: REASONS.get(k, k) for k in exclusions(grid)},
        "episodes": {k: {"count": len(v), "hours": round(sum(e.hours for e in v), 2)} for k, v in by_kind.items()},
        "open_windows": {"count": opened, "hours": round(open_h, 2)},
        "secondary_hours": secondary_hours(grid), "discontinuities": grid.discontinuities,
        "heat_basis": grid.heat_basis, "solar_basis": grid.solar_basis, "inferred_demand_hours": round(inferred_h, 2),
        "reference": s.reference, "outdoor_share": s.outdoor_share,
        "outdoor_hours": round(sum(step_h for o, ok in zip(grid.outdoor, s.usable, strict=True)
                                   if ok and o is not None), 2),
    }


def _binned(eps: list[Episode], key: Callable[[Episode], float | None], edges: Sequence[float], label: str
            ) -> list[dict[str, Any]]:
    out = []
    bounds = [-math.inf, *edges, math.inf]
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        rates = [e.rate for e in eps if (k := key(e)) is not None and lo <= k < hi]
        if len(rates) >= 2:
            name = f"< {hi:g}" if lo == -math.inf else f"≥ {lo:g}" if hi == math.inf else f"{lo:g}–{hi:g}"
            out.append({"bin": f"{label} {name} °C", "episodes": len(rates),
                        "median_rate_c_per_h": round(statistics.median(rates), 3)})
    return out


def _residual(grid: RoomGrid) -> dict[str, Any] | None:
    """What the room did in the 30 minutes after a device reported that heating stopped (confirmed passive)."""
    changes = []
    for i in range(1, len(grid.times) - BLOCK_STEPS):
        if (grid.heat[i - 1] or 0) > 0 and grid.heat[i] == 0 and all(
                grid.heat[j] == 0 and grid.why[j] == "usable" for j in range(i, i + BLOCK_STEPS + 1)) \
                and len({grid.source[j] for j in range(i, i + BLOCK_STEPS + 1)}) == 1:
            changes.append(grid.temp[i + BLOCK_STEPS] - grid.temp[i])  # type: ignore[operator]
    if len(changes) < 3:
        return None
    return {"stops": len(changes), "median_change_c": round(statistics.median(changes), 3),
            "share_warming": round(sum(1 for c in changes if c > 0.05) / len(changes), 3)}


# --------------------------------------------------------------- the model
LEVELS = {"none": 0, "low": 1, "moderate": 2, "high": 3}


def _param_level(value: float, se: float, cv: float, cap: str) -> str:
    rse = se / abs(value) if value else math.inf
    if rse <= 0.15 and cv <= 0.2:
        level = "high"
    elif rse <= 0.35 and cv <= 0.4:
        level = "moderate"
    else:
        level = "low"
    return min(level, cap, key=LEVELS.__getitem__)


def _cap(count: float, needs: dict[str, float]) -> str:
    return "high" if count >= needs["high"] else "moderate" if count >= needs["moderate"] else "low"


def _unidentifiable_doc(grid: RoomGrid, s: Series, evidence: dict[str, Any], selection: list[dict[str, Any]],
                        reason: str, now: float, min_history_days: int) -> dict[str, Any]:
    return {
        "format": tm.FORMAT, "schema": tm.SCHEMA, "algorithm": tm.ALGORITHM, "area_id": grid.area_id,
        "trained_at": _iso(now), "status": "unidentifiable", "preliminary": evidence["usable_days"] < min_history_days,
        "structure": {"reference": s.reference, "reference_c": s.reference_c, "heating": None, "solar": False,
                      "night": False, "coupling": []},
        "parameters": {}, "dynamics": {"lag_min": 0, "tau_min": 0}, "evidence": evidence, "selection": selection,
        "validation": None, "confidence": {"level": "none", "score": 0, "factors": []}, "uncertainty": {},
        "explanations": [reason], "reason": reason,
        "data": {"from": grid.times[0] if grid.times else None, "to": grid.times[-1] if grid.times else None,
                 "grid_s": GRID_S, "block_s": BLOCK_STEPS * GRID_S, "min_history_days": min_history_days},
    }


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _document(grid: RoomGrid, s: Series, st: Structure, fit: Fit, model: tm.ThermalModel, validation: dict[str, Any],
              stability: dict[str, Any], dynamics: dict[str, Any], evidence: dict[str, Any], eps: list[Episode],
              selection: list[dict[str, Any]], term_reasons: dict[str, str], heating_reason: str | None, *,
              now: float, min_history_days: int, split: float, passive: tm.ThermalModel | None) -> dict[str, Any]:
    cv = stability.get("cv", {})
    ep = evidence["episodes"]
    heat_cap = _cap(ep["heating"]["count"], EPISODES_FOR)
    passive_cap = _cap(ep["passive"]["hours"] + ep["solar"]["hours"], PASSIVE_HOURS_FOR)
    solar_cap = _cap(ep["solar"]["count"], EPISODES_FOR)
    inferred = grid.heat_basis == "inferred"
    params: dict[str, tm.Param] = {}

    def fitted(col: str, unit: str, cap: str, **kw: Any) -> tm.Param:
        value, se = fit.coef[col], fit.se.get(col, math.inf)
        return tm.Param(value, unit, True, se=se if math.isfinite(se) else None,
                        confidence=_param_level(value, se, cv.get(col, math.inf), cap), **kw)

    k_name = "outdoor loss coefficient" if s.reference == "outdoor" else "relaxation rate"
    fixed_cap = "low" if s.reference == "fixed" else "high"
    no_relaxation = s.reference == "fixed" and fit.coef["ref"] <= 1e-4
    params["k_per_h"] = tm.Param(None, "1/h", False, reason=(
        "Without an outdoor temperature the room's heat loss could not be measured; the model keeps only the "
        "heating and baseline effects")) if no_relaxation else \
        fitted("ref", "1/h", min(passive_cap, fixed_cap, key=LEVELS.__getitem__), basis="observed",
                               text=f"The {k_name}: the room moves towards "
                                    f"{'the outdoor temperature' if s.reference == 'outdoor' else 'its usual level'} by "
                                    f"this share of the difference per hour")
    params["baseline_c_per_h"] = fitted("const", "°C/h", passive_cap, basis="observed",
                                        text="Warming the room gets without its own heating (other rooms, people, "
                                             "appliances), at no difference to the reference")
    if st.heating and inferred:
        params["heating_c_per_h"] = tm.Param(None, "°C/h", False, reason=(
            "The heating devices do not report whether they are heating, so a confirmed heating rate cannot be "
            "measured (see the inferred estimate)"))
    if st.heating:
        name = "heating_inferred_c_per_h" if inferred else "heating_c_per_h"
        params[name] = fitted("heat", "°C/h", "low" if inferred else heat_cap, basis="inferred" if inferred else
                              "reported", bound="heating_c_per_h",
                              extra={"devices": grid.devices},
                              text=("Warming at full heating on top of the losses"
                                    if len(grid.devices) <= 1 else
                                    f"Warming with all {len(grid.devices)} heating devices heating, on top of the "
                                    f"losses (each device that heats adds its share)")
                                   + (" (from setpoints above the room temperature: the devices do not report "
                                      "whether they heat)" if inferred else " while the devices report heating"))
    else:
        reason = heating_reason or term_reasons.get("heating") or "Not identified"
        params["heating_c_per_h"] = tm.Param(None, "°C/h", False, reason=reason)
    if grid.solar is None:
        params["solar_c_per_h"] = tm.Param(None, "°C/h", False, reason=(
            "No window direction is known for this room (Curtains → window direction): direct sun cannot be told "
            "apart from daytime activity and weather"))
    elif st.solar:
        params["solar_c_per_h"] = fitted("solar", "°C/h", solar_cap, basis=grid.solar_basis,
                                         text="Warming with the sun head-on on the windows, covers open"
                                              + (", as bright as observed" if "measured" in grid.solar_basis
                                                 else " (an average over the observed weather: clouds are not measured)"))
    else:
        params["solar_c_per_h"] = tm.Param(None, "°C/h", False, reason=term_reasons.get("solar") or "Not identified")
    if st.night:
        params["night_c_per_h"] = fitted("night", "°C/h", min(passive_cap, fixed_cap, key=LEVELS.__getitem__),
                                         basis="observed",
                                         text="Difference of the baseline at night (22:00–06:00)")
    else:
        params["night_c_per_h"] = tm.Param(None, "°C/h", False, reason=term_reasons.get("night") or
                                           "No difference between day and night was measurable")
    for area in grid.neighbours:
        key = f"coupling:{area}"
        if area in st.coupling:
            params[key] = fitted(f"cpl:{area}", "1/h", passive_cap, basis="observed", bound="coupling_per_h",
                                 text=f"Exchange with {area}: the share of the temperature difference per hour")
        else:
            params[key] = tm.Param(None, "1/h", False, bound="coupling_per_h", reason=term_reasons.get(
                f"cpl:{area}") or f"At most {MAX_NEIGHBOURS} zone rooms are tested")
    tc = model.time_constant_h if not no_relaxation else None
    params["time_constant_h"] = tm.Param(tc, "h", tc is not None, confidence=params["k_per_h"].confidence,
                                         reason=None if tc is not None else "The heat loss could not be measured",
                                         basis="derived", text="Thermal inertia: the time to cover 63 % of a step "
                                                               "towards a new balance")
    if model.k > 0 and s.reference == "outdoor":
        params["balance_offset_c"] = tm.Param(model.d / model.k, "°C", True, confidence=params["k_per_h"].confidence,
                                              basis="derived", text="Without its own heating (no sun, by day), the "
                                                                    "room settles this much above the outdoor "
                                                                    "temperature")
    if st.heating:
        delay_ok = dynamics.get("identifiable")
        params["heating_delay_min"] = tm.Param(float(st.lag + st.tau) if delay_ok else None, "min", bool(delay_ok),
                                               confidence="low" if delay_ok else "none", basis="observed",
                                               reason=None if delay_ok else dynamics.get("reason"),
                                               text="Time from reported heating to 63 % of its warming effect")
        if delay_ok and st.tau > 0:
            residual = fit.coef["heat"] * st.tau / 60.0
            params["residual_warming_c"] = tm.Param(residual, "°C", True, confidence="low", basis="observed",
                                                    text=f"Warming that continues after full heating stops (the "
                                                         f"emitter cools over about {3 * st.tau} minutes)")
        else:
            params["residual_warming_c"] = tm.Param(None, "°C", False, reason="Not separable from the measurement "
                                                                              "noise" if st.heating else None)

    conf = _confidence(evidence, validation, params, stability, min_history_days)
    preliminary = evidence["usable_days"] < min_history_days
    head = validation.get("headline")
    skill = (validation["horizons"].get(head) or {}).get("skill") if head else None
    caps: list[tuple[str, str]] = []
    if inferred:
        caps.append(("moderate", "The heating activity is inferred, not reported: at most moderate confidence"))
    if no_relaxation:
        caps.append(("low", "The room's heat loss could not be measured: low confidence beyond a few hours"))
    if s.reference == "fixed":
        caps.append(("moderate", "Without an outdoor temperature the model cannot separate the weather from other "
                                 "daily causes: at most moderate confidence"))
    if fit.rho > 0.6:
        caps.append(("low", f"The fit leaves strongly correlated errors (autocorrelation {fit.rho:.2f}): something "
                            "the model does not include drives this room"))
    elif fit.rho > 0.4:
        caps.append(("moderate", f"The fit leaves correlated errors (autocorrelation {fit.rho:.2f}): at most "
                                 "moderate confidence"))
    hz = (validation["horizons"].get(head) or {}) if head else {}
    if hz.get("origins", 0) < 48 or validation.get("usable_hours", 0) < 24:
        caps.append(("moderate", "Less than a day of held-out data: at most moderate confidence"))
    if hz.get("origins", 0) < 24 or validation.get("usable_hours", 0) < 12:
        caps.append(("low", "Less than 12 hours of held-out data: low confidence"))
    for level, text in caps:
        if LEVELS[conf["level"]] > LEVELS[level]:
            conf["level"] = level
            conf["capped"] = text
    if head is None or skill is None or skill <= 0.05:
        conf["level"] = "low"
        status = "insufficient" if preliminary else "unidentifiable"
    elif preliminary:
        conf["level"] = "low"
        conf["capped"] = f"Less than {min_history_days} days of usable history: at most low confidence"
        status = "insufficient"
    else:
        status = conf["level"]
    heat_eps = [e for e in eps if e.kind == "heating"]
    passive_eps = [e for e in eps if e.kind in ("passive", "solar")]
    doc = {
        "format": tm.FORMAT, "schema": tm.SCHEMA, "algorithm": tm.ALGORITHM, "area_id": grid.area_id,
        "trained_at": _iso(now), "status": status, "preliminary": preliminary,
        "structure": {"reference": s.reference, "reference_c": s.reference_c,
                      "heating": ("inferred" if inferred else "reported") if st.heating else None,
                      "solar": st.solar, "night": st.night, "coupling": list(st.coupling)},
        "parameters": {k: v.as_dict() for k, v in params.items()},
        "dynamics": {"lag_min": st.lag, "tau_min": st.tau, **dynamics},
        "data": {"from": grid.times[0], "to": grid.times[-1] + GRID_S, "split": split,
                 "train": {"from": grid.times[0], "to": split, "blocks": fit.n},
                 "test": {"from": split, "to": grid.times[-1] + GRID_S},
                 "grid_s": GRID_S, "block_s": BLOCK_STEPS * GRID_S, "min_history_days": min_history_days},
        "fit": {"rmse_c_per_h": round(math.sqrt(fit.sse / max(1, fit.n)), 4), "blocks": fit.n,
                "residual_autocorrelation": round(fit.rho, 3), "at_zero": sorted(fit.at_zero)},
        "evidence": {**evidence,
                     "heating_rates": _binned(heat_eps, lambda e: None if e.outdoor is None else e.temp_start - e.outdoor,
                                              (10, 15, 20), "indoor − outdoor")
                     + _binned(heat_eps, lambda e: e.temp_start, (18, 20, 22), "start"),
                     "cooling_rates": _binned(passive_eps, lambda e: None if e.outdoor is None
                                              else e.temp_start - e.outdoor, (10, 15, 20), "indoor − outdoor"),
                     "heating_median_rate_c_per_h": round(statistics.median(e.rate for e in heat_eps), 3)
                     if len(heat_eps) >= 3 else None,
                     "passive_median_rate_c_per_h": round(statistics.median(e.rate for e in passive_eps), 3)
                     if len(passive_eps) >= 3 else None,
                     "after_heating": _residual(grid)},
        "selection": selection, "stability": stability, "validation": validation,
        "confidence": conf, "uncertainty": validation.get("uncertainty", {}),
        "baselines": {"passive_available": passive is not None},
    }
    doc["explanations"] = explain(doc)
    return doc


def _confidence(evidence: dict[str, Any], validation: dict[str, Any], params: dict[str, tm.Param],
                stability: dict[str, Any], min_history_days: int) -> dict[str, Any]:
    """A 0–100 score from named factors, and the qualitative level it supports. Each factor is shown."""
    ep = evidence["episodes"]
    head = validation.get("headline")
    hz = validation["horizons"].get(head) if head else None
    skill = hz.get("skill") if hz else None
    # The baseline is left out: it is often near zero, so its relative error says nothing about the model.
    fitted = [p for k, p in params.items() if p.available and k != "baseline_c_per_h"
              and p.basis in ("observed", "reported", "inferred", "geometry", "geometry+measured", "geometry+cloud")]
    ident = statistics.fmean({"high": 1.0, "moderate": 0.6, "low": 0.3}.get(p.confidence, 0.0) for p in fitted) \
        if fitted else 0.0
    cvs = [v for k, v in (stability.get("cv") or {}).items() if math.isfinite(v) and k != "const"]
    stable = statistics.fmean(max(0.0, 1 - v) for v in cvs) if cvs else 0.0
    quality = evidence["usable_hours"] / evidence["observed_hours"] if evidence["observed_hours"] else 0.0
    heating_ok = ep["heating"]["count"] if evidence["heat_basis"] in ("reported", "mixed") else 0
    factors = [
        ("history", min(1.0, evidence["usable_days"] / max(1, min_history_days)), 0.15,
         f"{evidence['usable_days']:.1f} of {min_history_days} days of usable history"),
        ("episodes", statistics.fmean([min(1.0, heating_ok / EPISODES_FOR["high"]),
                                       min(1.0, (ep["passive"]["hours"] + ep["solar"]["hours"]) / 72.0)]), 0.15,
         f"{ep['heating']['count']} confirmed heating episodes, "
         f"{ep['passive']['hours'] + ep['solar']['hours']:.0f} h of passive cooling"),
        ("sensor_quality", min(1.0, quality), 0.10, f"{quality * 100:.0f} % of the observed time was usable"),
        ("identifiability", ident, 0.20, "How precisely the fitted parameters are determined"),
        ("stability", stable, 0.15, f"Parameter agreement across {stability.get('windows', 0)} training windows"),
        ("validation", max(0.0, min(1.0, (skill or 0.0) / 0.4)), 0.25,
         f"Held-out {head} min error {hz['model']['mae']:.2f} °C vs. persistence {hz['persistence']['mae']:.2f} °C"
         if hz else "No held-out validation"),
    ]
    score = round(100 * sum(v * w for _k, v, w, _t in factors))
    val_hours = validation.get("usable_hours", 0)
    if skill is None or skill <= 0.05:
        level = "low"
    elif score >= 75 and skill >= 0.25 and val_hours >= 24:
        level = "high"
    elif score >= 55 and skill >= 0.10:
        level = "moderate"
    else:
        level = "low"
    return {"level": level, "score": score,
            "factors": [{"key": k, "value": round(v, 3), "weight": w, "text": t} for k, v, w, t in factors],
            "note": "The score is a weighted summary of the factors; the uncertainty band comes from the held-out "
                    "errors themselves."}


def explain(doc: dict[str, Any]) -> list[str]:
    """Plain-language statements, each built from a learned or measured value of ``doc``."""
    out: list[str] = []
    ev = doc["evidence"]
    p = doc["parameters"]
    out.append(f"This room has {ev['usable_days']:.1f} days ({ev['usable_hours']:.0f} hours) of usable temperature "
               f"history out of {ev['calendar_days']:.1f} calendar days observed.")
    heat = p.get("heating_c_per_h") or {}
    inferred = p.get("heating_inferred_c_per_h") or {}
    if heat.get("available"):
        net = ev.get("heating_median_rate_c_per_h")
        n = len(heat.get("devices") or [])
        full = "At full heating" if n <= 1 else f"With all {n} heating devices heating"
        out.append(f"{full} the room gains about {heat['value']:.2f} °C/hour on top of its losses"
                   + (f"; observed heating episodes raised it by a median {net:.2f} °C/hour under the measured "
                      f"conditions." if net is not None else "."))
    elif inferred.get("available"):
        out.append(f"The heating response is inferred from setpoints (about {inferred['value']:.2f} °C/hour at full "
                   "demand), because the devices do not report whether they are heating. Its confidence is limited.")
    elif heat.get("reason"):
        out.append(f"Heating response unavailable: {heat['reason'][:1].lower() + heat['reason'][1:]}.")
    delay = p.get("heating_delay_min") or {}
    if delay.get("available"):
        out.append(f"Warming follows {'inferred heating demand' if inferred.get('available') else 'reported heating'} with a delay of about {delay['value']:.0f} minutes"
                   + (f" and continues by about {p['residual_warming_c']['value']:.2f} °C after the heating stops."
                      if (p.get("residual_warming_c") or {}).get("available") else "."))
    k = p.get("k_per_h") or {}
    tc = p.get("time_constant_h") or {}
    if doc["structure"]["reference"] == "outdoor" and k.get("available"):
        out.append(f"Outdoor influence is measurable: the room loses about {k['value'] * 100:.1f} % of its "
                   f"difference to the outdoor temperature per hour (time constant about {tc.get('value') or 0:.0f} "
                   "hours).")
        bal = p.get("balance_offset_c") or {}
        if bal.get("available"):
            out.append(f"Without its own heating it would settle about {bal['value']:.1f} °C above the outdoor "
                       "temperature (heat from the rest of the house and from use).")
    elif not k.get("available"):
        share = ev.get("outdoor_share") or 0
        out.append((f"The outdoor temperature was known for only {share * 100:.0f} % of the training time, too little "
                    "to use" if share else "No usable outdoor temperature") + ": neither the outdoor influence nor the "
                   "room's heat loss could be measured, so the model only holds the heating and baseline effects it "
                   "observed.")
    elif k.get("available") and ev.get("outdoor_share"):
        out.append(f"The outdoor temperature was known for only {ev['outdoor_share'] * 100:.0f} % of the training "
                   "time, too little to estimate the outdoor influence; the model uses a simpler relaxation towards "
                   f"the room's usual level (time constant about {tc.get('value') or 0:.0f} hours).")
    elif k.get("available"):
        out.append("No outdoor temperature is configured, so the outdoor influence could not be estimated; the "
                   f"model uses a simpler relaxation towards the room's usual level (time constant about "
                   f"{tc.get('value') or 0:.0f} hours).")
    solar = p.get("solar_c_per_h") or {}
    if solar.get("available"):
        out.append(f"Direct sun on the windows adds up to about {solar['value']:.2f} °C/hour when it shines head-on "
                   "with the covers open.")
    else:
        why = solar.get("reason") or "not identified"
        out.append(f"Solar gain is not separated: {why[:1].lower() + why[1:]}.")
    night = p.get("night_c_per_h") or {}
    if night.get("available"):
        out.append(f"At night the baseline differs by {night['value']:+.2f} °C/hour.")
    for key, value in p.items():
        if key.startswith("coupling:") and value.get("available"):
            out.append(f"Heat exchange with {key[9:]} is measurable ({value['value'] * 100:.1f} % of the temperature "
                       "difference per hour).")
    if any(key.startswith("coupling:") and not value.get("available") for key, value in p.items()):
        out.append("Coupling with the other rooms of the thermal zone could not be measured separately; the rooms "
                   "are modelled with their own measurements.")
    val = doc.get("validation") or {}
    head = val.get("headline")
    if head:
        h = val["horizons"][head]
        hours = int(head) // 60
        out.append(f"The learned model's {hours}-hour validation error is {h['model']['mae']:.2f} °C MAE on "
                   f"{h['origins']} held-out periods it never saw (simulated with the outdoor temperature, heating "
                   f"and sun that actually occurred, on periods with closed windows); the persistence baseline is "
                   f"{h['persistence']['mae']:.2f} °C MAE.")
    if ev.get("secondary_hours"):
        out.append(f"For {ev['secondary_hours']:.0f} usable hours the primary sensor was not usable and the "
                   "secondary sensors were used instead.")
    if ev["open_windows"]["count"]:
        out.append(f"{ev['open_windows']['count']} open-window or open-door periods ({ev['open_windows']['hours']:.1f} "
                   "hours, plus 30 minutes after each) were left out.")
    if ev.get("discontinuities"):
        out.append(f"{ev['discontinuities']} sudden temperature jumps were treated as sensor events, not heat.")
    return out
