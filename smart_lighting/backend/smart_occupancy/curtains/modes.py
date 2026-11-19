"""Global Curtain Modes (0.6.4, the user's Phase 4.6.3): which preset a mode uses, per cover.

Pure logic, no Home Assistant calls. This is **not** a scheduler: the automatic
controller (``logic.py``) still decides one condition per window from its own
evidence; this module only answers "for this window and this condition, which
position does each of its covers get, and where does that come from?".

For one cover (layer) of a window and one mode the levels are tried from the
most specific down, and the first one that says anything wins:

1. **Cover override** (``Window.cover_modes``) - a preset of that cover, or
   No action;
2. **Window override** (``Window.conditions``, the 0.6.3 mapping) - a preset of
   the window (its target for this cover; a preset may leave a cover
   unchanged), or No action;
3. **Room override** (``CurtainConfig.room_modes``) - a preset id looked up in
   the window, or No action;
4. **Global Modes** (``CurtainConfig.modes``) - an assignment of this cover or
   of the whole window (never both in one mode);
5. nothing: **no assignment means no action**, the cover keeps its position.

A reference whose preset no longer exists is reported and acts as No action;
it never falls through to a lower level. Glare and Excess heat use the Direct
sunlight (Sun Protection) mapping of a cover that has nothing at all for them
(the 0.6.3 fallback); Useful solar heating uses the Morning / Daytime mapping.
Nothing else ever falls back to another mode: a winning mode without an
assignment leaves the covers where they are.

Because every result is one value per layer, a window can never get two
targets for the same physical cover in one decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .config import (
    KIND_WORDS,
    MODE_WORDS,
    MODES,
    NO_ACTION,
    SUN_FALLBACK,
    CoverLayer,
    CurtainConfig,
    Preset,
    Window,
)

LEVELS = ("cover", "window", "room", "global")
SOURCE_WORDS = {"cover": "Cover override", "window": "Window override", "room": "Room override",
                "global": "Global Mode", None: "No assignment", "mixed": "Mixed"}


@dataclass(frozen=True, slots=True)
class LayerTarget:
    """What one cover gets in one mode."""

    layer_id: str
    state: str                 # preset / no_action / missing / nothing
    position: int | None = None
    source: str | None = None  # cover / window / room / global
    preset_id: str | None = None
    preset_name: str | None = None
    library: str | None = None  # "window" (a window preset) or "cover" (the cover's own preset)
    fallback: bool = False      # Glare / Excess heat using the Sun Protection mapping

    def as_dict(self) -> dict[str, Any]:
        return {"layer_id": self.layer_id, "state": self.state, "position": self.position, "source": self.source,
                "source_word": SOURCE_WORDS.get(self.source, self.source), "preset_id": self.preset_id,
                "preset_name": self.preset_name, "library": self.library, "fallback": self.fallback}


@dataclass(frozen=True, slots=True)
class Effective:
    """The effective target of one window in one mode: per cover, plus one preset the executor can run."""

    mode: str
    state: str                       # target / no_action / nothing
    preset: Preset | None = None     # the window preset itself, or a combined per-cover target
    source: str | None = None        # cover / window / room / global / mixed
    layers: tuple[LayerTarget, ...] = ()
    fallback: bool = False
    problems: tuple[str, ...] = ()
    names: Mapping[str, str] = field(default_factory=dict)  # layer id -> its name (for texts)

    @property
    def has_target(self) -> bool:
        return self.state == "target"

    @property
    def preset_id(self) -> str | None:
        return self.preset.preset_id if self.preset is not None and self.has_target else None

    @property
    def source_word(self) -> str:
        return SOURCE_WORDS.get(self.source, self.source or "")

    @property
    def label(self) -> str:
        if self.has_target and self.preset is not None:
            return self.preset.name
        return "No action" if self.state == "no_action" else "No preset assigned"

    def unassigned(self) -> list[str]:
        """Names of covers this mode leaves alone because nothing at all is assigned to them."""
        return [self.names.get(t.layer_id, t.layer_id) for t in self.layers if t.state == "nothing"]

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "state": self.state, "label": self.label, "preset_id": self.preset_id,
                "positions": dict(self.preset.positions) if self.preset is not None and self.has_target else {},
                "source": self.source, "source_word": self.source_word, "fallback": self.fallback,
                "layers": [t.as_dict() for t in self.layers], "problems": list(self.problems)}


NOTHING = Effective("", "nothing")


SHORT_KIND = {"blackout": "Blackout", "sheer": "Sheer", "shade": "Shade", "custom": "Cover"}


def layer_name(layer: CoverLayer) -> str:
    return layer.label or SHORT_KIND.get(layer.kind, KIND_WORDS.get(layer.kind, layer.kind))


# ------------------------------------------------------------------ one cover
def resolve_layer(config: CurtainConfig, window: Window, layer: CoverLayer, mode: str, *,
                  legacy: bool | None = None) -> LayerTarget:
    """The first level that says anything about this cover in this mode (see the module docstring).

    0.6.6: once the configuration is centralized only Global Modes count. The room, window and cover levels
    are read only for a store that has not been migrated yet (``legacy``), so its behaviour is unchanged."""
    lid = layer.layer_id
    if legacy is None:
        legacy = not config.centralized
    if not legacy:
        return _global_layer(config, window, layer, mode)
    value = window.cover_modes.get(lid, {}).get(mode) if window.layered else None
    if value == NO_ACTION:
        return LayerTarget(lid, "no_action", source="cover")
    if value:
        cp = layer.preset(value)
        if cp is None:
            return LayerTarget(lid, "missing", source="cover", preset_id=value, library="cover")
        return LayerTarget(lid, "preset", cp.position, "cover", cp.preset_id, cp.name, "cover")
    for source, value in (("window", window.conditions.get(mode)),
                          ("room", config.room_modes.get(window.area_id or "", {}).get(mode)
                           if window.area_id else None)):
        if value == NO_ACTION:
            return LayerTarget(lid, "no_action", source=source)
        if value:
            p = window.preset(value)
            if p is None:
                return LayerTarget(lid, "missing", source=source, preset_id=value, library="window")
            return LayerTarget(lid, "preset", p.positions.get(lid), source, p.preset_id, p.name, "window")
    return _global_layer(config, window, layer, mode)


def _global_layer(config: CurtainConfig, window: Window, layer: CoverLayer, mode: str) -> LayerTarget:
    lid = layer.layer_id
    items = config.modes.get(mode, ())
    a = next((x for x in items if x.window_id == window.window_id and x.layer_id == lid and window.layered), None)
    if a is not None:
        cp = layer.preset(a.preset_id)
        if cp is None:
            return LayerTarget(lid, "missing", source="global", preset_id=a.preset_id, library="cover")
        return LayerTarget(lid, "preset", cp.position, "global", cp.preset_id, cp.name, "cover")
    a = next((x for x in items if x.window_id == window.window_id and x.layer_id is None), None)
    if a is not None:
        p = window.preset(a.preset_id)
        if p is None:
            return LayerTarget(lid, "missing", source="global", preset_id=a.preset_id, library="window")
        return LayerTarget(lid, "preset", p.positions.get(lid), "global", p.preset_id, p.name, "window")
    return LayerTarget(lid, "nothing")


# ----------------------------------------------------------------- one window
def effective(config: CurtainConfig, window: Window, condition: str, phase: str | None = None, *,
              legacy: bool | None = None) -> Effective:
    """The effective target of a window for a decided condition (``phase`` for Useful solar heating)."""
    if condition in ("waiting", "manual_override"):
        return Effective(condition, "nothing")
    mode = (phase or "day") if condition == "solar_heating" else condition
    if mode not in MODES:
        return Effective(condition, "nothing")
    targets: list[LayerTarget] = []
    for layer in window.layers:
        t = resolve_layer(config, window, layer, mode, legacy=legacy)
        if t.state == "nothing" and mode in SUN_FALLBACK:
            sun = resolve_layer(config, window, layer, "sun", legacy=legacy)
            if sun.state != "nothing":
                t = LayerTarget(sun.layer_id, sun.state, sun.position, sun.source, sun.preset_id, sun.preset_name,
                                sun.library, True)
        targets.append(t)
    return combine(window, condition, tuple(targets))


def combine(window: Window, mode: str, targets: tuple[LayerTarget, ...]) -> Effective:
    names = {x.layer_id: layer_name(x) for x in window.layers}
    problems = tuple(f"{names.get(t.layer_id, t.layer_id)}: the preset {t.preset_id} no longer exists "
                     f"({SOURCE_WORDS[t.source]}); nothing is sent for it" for t in targets if t.state == "missing")
    positions = {t.layer_id: (t.position if t.state == "preset" else None) for t in targets}
    has_target = any(v is not None for v in positions.values())
    configured = any(t.state != "nothing" for t in targets)
    fallback = any(t.fallback for t in targets)
    used = [t for t in targets if t.state == "preset"]
    sources = {t.source for t in targets if t.state != "nothing"}
    source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else None)
    if not has_target:
        return Effective(mode, "no_action" if configured else "nothing", None, source, targets, fallback, problems,
                         names)
    whole = {(t.library, t.preset_id) for t in used}
    if len(used) == len(targets) and len(whole) == 1 and next(iter(whole))[0] == "window":
        p = window.preset(used[0].preset_id)
        preset = Preset(p.preset_id, p.name, dict(p.positions))   # exactly the window's preset
    else:
        single = len(whole) == 1 and next(iter(whole))[0] == "window"
        name = used[0].preset_name if single else " · ".join(
            f"{names.get(t.layer_id, t.layer_id)}: {t.preset_name}" for t in used)
        key = "|".join(f"{t.layer_id}={t.library}:{t.preset_id}" for t in used)
        preset = Preset(f"mix:{key}", (name or "Combined")[:120], positions)
    return Effective(mode, "target", preset, source, targets, fallback, problems, names)


def mapping_effective(conditions: Mapping[str, str | None], condition: str, phase: str | None = None) -> Effective:
    """The same answer from a plain whole-window mapping (pure-logic callers and the 0.6.2 equality tests)."""
    if condition in ("waiting", "manual_override"):
        return Effective(condition, "nothing")
    mode = (phase or "day") if condition == "solar_heating" else condition
    value = conditions.get(mode)
    fallback = False
    if value is None and mode in SUN_FALLBACK:
        value, fallback = conditions.get("sun"), True
    if value == NO_ACTION:
        return Effective(condition, "no_action", fallback=fallback)
    if not value:
        return Effective(condition, "nothing")
    return Effective(condition, "target", Preset(value, value, {}), "window", fallback=fallback)


# -------------------------------------------------------------- the whole house
def window_matrix(config: CurtainConfig, window: Window) -> dict[str, Any]:
    """Every mode of one window: the whole-window result and each cover's (for the matrix and the card)."""
    out: dict[str, Any] = {}
    for mode in MODES:
        eff = effective(config, window, mode)
        out[mode] = eff.as_dict()
    return out


def own_value(config: CurtainConfig, window: Window, mode: str, level: str, layer_id: str | None = None) -> str | None:
    """What one level itself says (None = inherit / not assigned)."""
    if level == "cover":
        return window.cover_modes.get(layer_id or "", {}).get(mode)
    if level == "window":
        return window.conditions.get(mode)
    if level == "room":
        return config.room_modes.get(window.area_id or "", {}).get(mode) if window.area_id else None
    a = config.assignment(mode, window.window_id, layer_id)
    return a.preset_id if a else None


def hidden_by(config: CurtainConfig, window: Window, mode: str, layer_id: str | None) -> list[dict[str, Any]]:
    """The overrides that hide a Global Modes assignment of this target (each with what it sets)."""
    layers = [window.layer(layer_id)] if layer_id else list(window.layers)
    seen: dict[tuple[str, str | None], dict[str, Any]] = {}
    for layer in layers:
        if layer is None:
            continue
        t = resolve_layer(config, window, layer, mode)
        if t.source in (None, "global"):
            continue
        key = (t.source, layer.layer_id if t.source == "cover" else None)
        label = "No action" if t.state == "no_action" else t.preset_name or "a missing preset"
        seen.setdefault(key, {"level": t.source, "layer_id": key[1], "word": SOURCE_WORDS[t.source],
                              "label": label, "state": t.state,
                              "cover": layer_name(layer) if t.source == "cover" else None})
    return list(seen.values())


def overrides_of(config: CurtainConfig, window: Window) -> list[dict[str, Any]]:
    """Every window and cover override of a window (to show them and to move them to Global Modes)."""
    out = []
    for mode in MODES:
        value = window.conditions.get(mode)
        if value:
            p = window.preset(value)
            out.append({"mode": mode, "level": "window", "layer_id": None, "value": value,
                        "label": "No action" if value == NO_ACTION else p.name if p else "missing preset"})
        if window.layered:
            for layer in window.layers:
                value = window.cover_modes.get(layer.layer_id, {}).get(mode)
                if value:
                    cp = layer.preset(value)
                    out.append({"mode": mode, "level": "cover", "layer_id": layer.layer_id, "value": value,
                                "cover": layer_name(layer),
                                "label": "No action" if value == NO_ACTION else cp.name if cp else "missing preset"})
    return out


def describe(eff: Effective, mode: str) -> str:
    """ "Half shade — Global Morning", "No action — Explicit window override", "No preset assigned". """
    word = MODE_WORDS.get(mode, mode)
    if eff.state == "nothing":
        return "No preset assigned"
    if eff.problems and not eff.has_target:
        return f"Missing preset — {eff.source_word}"
    if eff.state == "no_action":
        if eff.source in ("cover", "window", "room"):
            return f"No action — Explicit {SOURCE_WORDS[eff.source].lower()}"
        return "No action — " + (f"Global {word}" if eff.source == "global" else eff.source_word)
    tail = f"Global {word}" if eff.source == "global" else eff.source_word
    if eff.fallback:
        tail += " (uses Sun Protection)"
    return f"{eff.label} — {tail}"
