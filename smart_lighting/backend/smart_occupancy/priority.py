"""Priority Inputs (0.6.8, Phase 4.8): an input that temporarily forces lights On.

Pure configuration and claim logic; the runtime is ``priority_runtime.py``.

A **Priority Input** watches one entity (a door contact ``binary_sensor``, an
``input_boolean``, or an explicitly chosen ``switch``) and, while it is
*active*, forces its target lights (lights or relay outputs this room manages)
**On**. Example: a magnetic contact on a cupboard door and the cupboard light.

Arming
------
A new input is **disabled**. It sends nothing until an administrator enables
it after reviewing what it would do (``priority/preview`` then
``priority/set`` with ``enabled`` and ``confirm``). A global **safety lock**
(``LightingConfig.priority_lock``) stops every Priority Input at once.

Precedence
----------
While active, the input's claim is the **winning rule** of each target, above
zones, brightness rules, the mood (Occupied and Vacant profiles), possible
occupancy and the room's occupancy: it works in a vacant room, in daylight,
in Manual mood and in an Observing room (like an Active wall switch: an
explicit, deliberately armed request). It does **not** bypass:

* an unavailable device, an excluded light or a member controlled through
  its group,
* an enabled automation that also controls the light (unacknowledged
  conflict),
* a manual change of the light - unless the input is explicitly authorized
  (``override_manual``) and the change was made *before* the input became
  active (a change made while it is active is always respected),
* capabilities: a Dumb Light / relay / on-off light just switches on; a
  dimmable light gets ``brightness_pct`` when set; a colour light gets the
  appearance policy (``none``, the room ``mood``'s colour, or a white
  ``kelvin``) only when it supports it.

Several inputs may claim one light (an active-request model): it stays On
while any of them is active; the brightness is the **highest** requested one
(an input without brightness asks for none), ties go to the input listed
first, and diagnostics name the winning request. One plan item - one command -
per light.

Polarity
--------
``active_when``: the state that means *active* (``on`` - e.g. door open - by
default; ``off`` for a normally closed contact that reports ``off`` when
open). ``unknown`` / ``unavailable`` are never read as open or closed: the
``unavailable`` policy decides (``hold`` the last state for ``grace`` seconds,
then release; ``release`` at once; ``keep`` until the input reports again).

Release
-------
When the input becomes inactive, after ``release_delay`` seconds its claim
ends and the light's **current** winning rule is applied through the normal
executor (not a snapshot of the light's earlier state). If no automatic rule
can take the light over (the room is Observing, Main Mood is Manual, or no
rule sets it), a light the input itself switched on is switched off again -
unless a person changed it meanwhile. ``release_manual``: a person's change
made while the input was active is kept (``respect``, default) or ended at
release so the current rule takes over (``resume``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_INPUTS = 16
MAX_TARGETS = 24
DELAY_MAX = 600.0
GRACE_MAX = 3600.0
DEFAULT_DEBOUNCE = 0.0
DEFAULT_RELEASE_DELAY = 2.0
DEFAULT_GRACE = 60.0
KELVIN_MIN = 1000
KELVIN_MAX = 10000
INPUT_DOMAINS = frozenset({"binary_sensor", "input_boolean", "switch"})


class Unavailable(StrEnum):
    HOLD = "hold"          # keep the last state for ``grace`` seconds, then release
    RELEASE = "release"    # release at once
    KEEP = "keep"          # keep the last state until the input reports again (explicit)


class Appearance(StrEnum):
    NONE = "none"          # on (and brightness) only
    MOOD = "mood"          # the room mood's colour / colour temperature, where supported
    KELVIN = "kelvin"      # a white colour temperature, where supported


class ReleaseManual(StrEnum):
    RESPECT = "respect"    # a person's change made while active is kept after release
    RESUME = "resume"      # ended at release: the current rule takes over


def _float(value: Any, low: float, high: float, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    number = min(max(number, low), high)
    return int(number) if float(number).is_integer() else round(number, 2)


def _int(value: Any, low: int, high: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(min(max(round(float(value)), low), high))
    except (TypeError, ValueError):
        return None


def _enum(cls: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return cls(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class PriorityInput:
    """One Priority Input of a room (see the module docstring)."""

    id: str
    name: str
    entity_id: str | None = None
    enabled: bool = False
    active_when: str = "on"
    targets: tuple[str, ...] = ()
    brightness_pct: int | None = 100
    appearance: Appearance = Appearance.NONE
    kelvin: int | None = None
    debounce: float = DEFAULT_DEBOUNCE
    release_delay: float = DEFAULT_RELEASE_DELAY
    unavailable: Unavailable = Unavailable.HOLD
    grace: float = DEFAULT_GRACE
    override_manual: bool = False
    release_manual: ReleaseManual = ReleaseManual.RESPECT
    enabled_at: str | None = None
    enabled_by: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> PriorityInput | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            return None
        entity = data.get("entity_id")
        entity = entity if isinstance(entity, str) and "." in entity else None
        targets = tuple(dict.fromkeys(t for t in data.get("targets") or [] if isinstance(t, str) and "." in t))
        appearance = _enum(Appearance, data.get("appearance"), Appearance.NONE)
        return cls(
            id=data["id"], name=str(data.get("name") or data["id"]).strip()[:60] or data["id"],
            entity_id=entity,
            # (Never enabled without an input entity and a target.)
            enabled=data.get("enabled") is True and entity is not None and bool(targets),
            active_when="off" if data.get("active_when") == "off" else "on",
            targets=targets[:MAX_TARGETS],
            brightness_pct=_int(data.get("brightness_pct"), 1, 100) if "brightness_pct" in data else 100,
            appearance=appearance,
            kelvin=_int(data.get("kelvin"), KELVIN_MIN, KELVIN_MAX) if appearance == Appearance.KELVIN else None,
            debounce=_float(data.get("debounce"), 0, DELAY_MAX, DEFAULT_DEBOUNCE),
            release_delay=_float(data.get("release_delay"), 0, DELAY_MAX, DEFAULT_RELEASE_DELAY),
            unavailable=_enum(Unavailable, data.get("unavailable"), Unavailable.HOLD),
            grace=_float(data.get("grace"), 0, GRACE_MAX, DEFAULT_GRACE),
            override_manual=data.get("override_manual") is True,
            release_manual=_enum(ReleaseManual, data.get("release_manual"), ReleaseManual.RESPECT),
            enabled_at=data.get("enabled_at") if isinstance(data.get("enabled_at"), str) else None,
            enabled_by=data.get("enabled_by") if isinstance(data.get("enabled_by"), str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "entity_id": self.entity_id, "enabled": self.enabled,
            "active_when": self.active_when, "targets": list(self.targets), "brightness_pct": self.brightness_pct,
            "appearance": self.appearance.value, "kelvin": self.kelvin, "debounce": self.debounce,
            "release_delay": self.release_delay, "unavailable": self.unavailable.value, "grace": self.grace,
            "override_manual": self.override_manual, "release_manual": self.release_manual.value,
            "enabled_at": self.enabled_at, "enabled_by": self.enabled_by,
        }

    def reading(self, state: str | None) -> bool | None:
        """True: active; False: inactive; None: unknown / unavailable (never read as open or closed)."""
        if state is None or state in ("unknown", "unavailable", ""):
            return None
        if state not in ("on", "off"):
            return None
        return (state == "on") == (self.active_when == "on")

    def describe(self) -> str:
        what = "On" if self.brightness_pct is None else f"On ({self.brightness_pct} % where dimmable)"
        return what


def inputs_from(data: Any) -> tuple[PriorityInput, ...]:
    out: list[PriorityInput] = []
    for item in data if isinstance(data, (list, tuple)) else []:
        pi = PriorityInput.from_dict(item)
        if pi is not None and all(p.id != pi.id for p in out):
            out.append(pi)
    return tuple(out[:MAX_INPUTS])


# ------------------------------------------------------------------- claims
@dataclass(slots=True)
class PriorityClaim:
    """Every active Priority Input that claims one light, and the request that wins."""

    entity_id: str
    inputs: list[PriorityInput] = field(default_factory=list)   # in configuration order
    since: dict[str, str | None] = field(default_factory=dict)  # input id -> when it became active

    @property
    def winner(self) -> PriorityInput:
        """The highest requested brightness (an input without brightness asks for none); ties: listed first."""
        return max(self.inputs, key=lambda p: (-1 if p.brightness_pct is None else p.brightness_pct,
                                               -self.inputs.index(p)))

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.inputs]

    def as_dict(self) -> dict[str, Any]:
        w = self.winner
        return {"entity_id": self.entity_id, "inputs": [p.id for p in self.inputs], "names": self.names,
                "winner": w.id, "winner_name": w.name, "brightness_pct": w.brightness_pct}


def claims_for(inputs: list[PriorityInput], since: dict[str, str | None]) -> dict[str, PriorityClaim]:
    """entity -> the claim of every active input that targets it (``inputs`` in configuration order)."""
    out: dict[str, PriorityClaim] = {}
    for pi in inputs:
        for entity_id in pi.targets:
            claim = out.setdefault(entity_id, PriorityClaim(entity_id))
            claim.inputs.append(pi)
            claim.since[pi.id] = since.get(pi.id)
    return out
