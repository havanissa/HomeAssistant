"""Curtain configuration: windows, cover layers, presets and settings (0.6.1).

Everything here is plain data with strict validation. Positions are percent
open (Home Assistant's cover convention: 0 = closed, 100 = fully open). A
preset target of ``None`` means "Leave unchanged".

Presets refer to a window's layers by their stable ``layer_id`` (not by entity
id), so renaming or replacing a cover entity keeps every preset. Windows and
presets have stable ids as well; names are free text and can change at any
time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
import math
import secrets
from typing import Any

COVER_KINDS: tuple[str, ...] = ("blackout", "sheer", "shade", "custom")
KIND_WORDS = {"blackout": "Blackout", "sheer": "Semitransparent / sheer", "shade": "Shade", "custom": "Custom"}

# Operating modes of a window. Automatic is part of the model for the next
# phase, but this build refuses to select it (see PERMITTED_MODES).
WINDOW_MODES: tuple[str, ...] = ("off", "observing", "automatic")
PERMITTED_MODES: frozenset[str] = frozenset({"off", "observing"})
DEFAULT_MODE = "observing"

COMPASS: dict[str, float] = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0, "SW": 225.0, "W": 270.0,
                             "NW": 315.0}

# Future automatic conditions (Phase 4.6.1) -> the preset each one will use.
CONDITIONS: tuple[str, ...] = ("morning", "day", "sun", "evening_privacy", "night", "sleep", "manual_override")
CONDITION_WORDS = {
    "morning": "Morning", "day": "Normal daytime", "sun": "Direct sunlight", "evening_privacy": "Evening privacy",
    "night": "Night", "sleep": "Sleep", "manual_override": "Manual override",
}

MAX_WINDOWS = 64
MAX_LAYERS = 6
MAX_PRESETS = 24
MAX_OBSTRUCTIONS = 4
NAME_MAX = 60

TOLERANCE_LIMITS = (0, 10)          # percentage points: "at the position" / "already there"
VERIFY_TIMEOUT_LIMITS = (20, 600)   # seconds to wait for the reported position
EXPOSURE_WIDTH_LIMITS = (5, 90)     # ± degrees either side of the window's bearing
ELEVATION_LIMITS = (-5.0, 60.0)


class CurtainConfigError(ValueError):
    """A configuration change was refused; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def new_id(prefix: str, taken: Iterable[str] = ()) -> str:
    taken = set(taken)
    while True:
        candidate = f"{prefix}{secrets.token_hex(4)}"
        if candidate not in taken:
            return candidate


def clean_name(value: Any, what: str = "Name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise CurtainConfigError("invalid_name", f"{what} must not be empty")
    name = " ".join(value.split())
    if len(name) > NAME_MAX:
        raise CurtainConfigError("invalid_name", f"{what} must be at most {NAME_MAX} characters")
    return name


def valid_position(value: Any) -> int | None:
    """A preset target: a whole number 0-100, or None (Leave unchanged). Anything else is refused."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value != int(value) or not 0 <= value <= 100:
        raise CurtainConfigError("invalid_position", "Positions must be whole numbers from 0 to 100 %")
    return int(value)


def normalize_bearing(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CurtainConfigError("invalid_bearing", "The direction must be 0-360°")
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise CurtainConfigError("invalid_bearing", "The direction must be 0-360°") from err
    if not math.isfinite(number) or not 0 <= number <= 360:
        raise CurtainConfigError("invalid_bearing", "The direction must be 0-360°")
    return round(number % 360, 1)


def compass_label(bearing: float | None) -> str | None:
    if bearing is None:
        return None
    return list(COMPASS)[int(((bearing % 360) + 22.5) // 45) % 8]


def _number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, low), high)


# ---------------------------------------------------------------- data model
@dataclass(frozen=True, slots=True)
class CoverLayer:
    layer_id: str
    entity_id: str
    kind: str = "custom"
    label: str | None = None  # optional own name, e.g. "Ljós"

    def as_dict(self) -> dict[str, Any]:
        return {"layer_id": self.layer_id, "entity_id": self.entity_id, "kind": self.kind, "label": self.label}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CoverLayer | None:
        layer_id, entity_id = data.get("layer_id"), data.get("entity_id")
        if not isinstance(layer_id, str) or not isinstance(entity_id, str) or not entity_id.startswith("cover."):
            return None
        kind = data.get("kind")
        label = data.get("label")
        return cls(layer_id, entity_id, kind if kind in COVER_KINDS else "custom",
                   label if isinstance(label, str) and label.strip() else None)


@dataclass(frozen=True, slots=True)
class Preset:
    preset_id: str
    name: str
    # layer_id -> target position (0-100) or None = Leave unchanged.
    positions: Mapping[str, int | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"preset_id": self.preset_id, "name": self.name, "positions": dict(self.positions)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], layers: Iterable[str]) -> Preset | None:
        preset_id, name = data.get("preset_id"), data.get("name")
        if not isinstance(preset_id, str) or not isinstance(name, str) or not name.strip():
            return None
        raw = data.get("positions") if isinstance(data.get("positions"), Mapping) else {}
        positions: dict[str, int | None] = {}
        for layer in layers:
            try:
                positions[layer] = valid_position(raw.get(layer))
            except CurtainConfigError:
                positions[layer] = None  # a damaged value is never guessed: Leave unchanged
        return cls(preset_id, name.strip()[:NAME_MAX], positions)

    @property
    def targets(self) -> dict[str, int]:
        return {k: v for k, v in self.positions.items() if v is not None}


@dataclass(frozen=True, slots=True)
class Obstruction:
    """Between two azimuths the sun reaches the window only above ``min_elevation`` (a hill, a house, trees)."""

    from_azimuth: float
    to_azimuth: float
    min_elevation: float

    def as_dict(self) -> dict[str, Any]:
        return {"from_azimuth": self.from_azimuth, "to_azimuth": self.to_azimuth, "min_elevation": self.min_elevation}

    @classmethod
    def from_dict(cls, data: Any) -> Obstruction:
        if not isinstance(data, Mapping):
            raise CurtainConfigError("invalid_obstruction", "An obstruction needs from, to and a minimum elevation")
        try:
            start = normalize_bearing(data.get("from_azimuth"))
            end = normalize_bearing(data.get("to_azimuth"))
        except CurtainConfigError:
            start = end = None
        if start is None or end is None:
            raise CurtainConfigError("invalid_obstruction", "An obstruction needs a from and a to direction (0-360°)")
        elevation = data.get("min_elevation")
        if isinstance(elevation, bool) or not isinstance(elevation, (int, float)) or not 0 <= elevation <= 90:
            raise CurtainConfigError("invalid_obstruction", "The obstruction's elevation must be 0-90°")
        return cls(start, end, round(float(elevation), 1))


@dataclass(frozen=True, slots=True)
class SolarSettings:
    exposure_width: int = 70        # the sun can shine in within ± this many degrees of the bearing
    min_elevation: float = 3.0      # below this the sun is too low to matter
    obstructions: tuple[Obstruction, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"exposure_width": self.exposure_width, "min_elevation": self.min_elevation,
                "obstructions": [o.as_dict() for o in self.obstructions]}

    @classmethod
    def from_dict(cls, data: Any) -> SolarSettings:
        data = data if isinstance(data, Mapping) else {}
        obstructions: list[Obstruction] = []
        for item in data.get("obstructions") or ():
            try:
                obstructions.append(Obstruction.from_dict(item))
            except CurtainConfigError:
                continue
        return cls(
            exposure_width=int(_number(data.get("exposure_width"), 70, *EXPOSURE_WIDTH_LIMITS)),
            min_elevation=round(_number(data.get("min_elevation"), 3.0, *ELEVATION_LIMITS), 1),
            obstructions=tuple(obstructions[:MAX_OBSTRUCTIONS]),
        )


@dataclass(frozen=True, slots=True)
class Window:
    window_id: str
    name: str
    area_id: str | None = None
    layers: tuple[CoverLayer, ...] = ()
    direction: str | None = None   # compass point, or None when an exact bearing is set
    bearing: float | None = None   # exact degrees (0 = north, 90 = east); overrides the compass point
    mode: str = DEFAULT_MODE
    solar_protection: bool = False
    privacy: bool = False
    sleep_protection: bool = False
    solar: SolarSettings = field(default_factory=SolarSettings)
    presets: tuple[Preset, ...] = ()
    selected_preset: str | None = None
    conditions: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def effective_bearing(self) -> float | None:
        if self.bearing is not None:
            return self.bearing
        return COMPASS.get(self.direction) if self.direction else None

    @property
    def layer_ids(self) -> list[str]:
        return [layer.layer_id for layer in self.layers]

    @property
    def entity_ids(self) -> list[str]:
        return [layer.entity_id for layer in self.layers]

    def layer(self, layer_id: str) -> CoverLayer | None:
        return next((layer for layer in self.layers if layer.layer_id == layer_id), None)

    def preset(self, preset_id: str | None) -> Preset | None:
        return next((p for p in self.presets if p.preset_id == preset_id), None) if preset_id else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id, "name": self.name, "area_id": self.area_id,
            "layers": [layer.as_dict() for layer in self.layers],
            "direction": self.direction, "bearing": self.bearing, "mode": self.mode,
            "solar_protection": self.solar_protection, "privacy": self.privacy,
            "sleep_protection": self.sleep_protection, "solar": self.solar.as_dict(),
            "presets": [p.as_dict() for p in self.presets], "selected_preset": self.selected_preset,
            "conditions": {c: self.conditions.get(c) for c in CONDITIONS},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Window | None:
        window_id, name = data.get("window_id"), data.get("name")
        if not isinstance(window_id, str) or not isinstance(name, str) or not name.strip():
            return None
        layers: list[CoverLayer] = []
        for item in data.get("layers") or ():
            if isinstance(item, Mapping) and (layer := CoverLayer.from_dict(item)) is not None \
                    and all(x.layer_id != layer.layer_id and x.entity_id != layer.entity_id for x in layers):
                layers.append(layer)
        layer_ids = [x.layer_id for x in layers]
        presets: list[Preset] = []
        for item in data.get("presets") or ():
            if isinstance(item, Mapping) and (preset := Preset.from_dict(item, layer_ids)) is not None \
                    and all(p.preset_id != preset.preset_id for p in presets):
                presets.append(preset)
        preset_ids = {p.preset_id for p in presets}
        direction = data.get("direction")
        try:
            bearing = normalize_bearing(data.get("bearing"))
        except CurtainConfigError:
            bearing = None
        mode = data.get("mode")
        raw_conditions = data.get("conditions") if isinstance(data.get("conditions"), Mapping) else {}
        selected = data.get("selected_preset")
        area_id = data.get("area_id")
        return cls(
            window_id=window_id,
            name=name.strip()[:NAME_MAX],
            area_id=area_id if isinstance(area_id, str) and area_id else None,
            layers=tuple(layers[:MAX_LAYERS]),
            direction=direction if direction in COMPASS and bearing is None else None,
            bearing=bearing,
            # A stored mode this build does not permit (Automatic) is kept as Observing: nothing moves on its own.
            mode=mode if mode in PERMITTED_MODES else DEFAULT_MODE,
            solar_protection=data.get("solar_protection") is True,
            privacy=data.get("privacy") is True,
            sleep_protection=data.get("sleep_protection") is True,
            solar=SolarSettings.from_dict(data.get("solar")),
            presets=tuple(presets[:MAX_PRESETS]),
            selected_preset=selected if selected in preset_ids else None,
            conditions={c: (v if (v := raw_conditions.get(c)) in preset_ids else None) for c in CONDITIONS},
        )


@dataclass(frozen=True, slots=True)
class CurtainSettings:
    """Settings for every window."""

    tolerance: int = 3              # a cover within this many points of a target is "at" it (and is not moved)
    verify_timeout: int = 120       # seconds to wait for the reported position after a command
    # The house's compensated sun angle (0 = the sun faces the south-facing windows). A calibrated
    # reference only: shown, validated against the standard azimuth, never used to move anything.
    sun_angle_entity: str | None = "sensor.sun_angle"
    # Standard solar azimuth / elevation: "sun.sun" reads its attributes; a sensor reads its state.
    azimuth_entity: str = "sun.sun"
    elevation_entity: str = "sun.sun"

    def as_dict(self) -> dict[str, Any]:
        return {"tolerance": self.tolerance, "verify_timeout": self.verify_timeout,
                "sun_angle_entity": self.sun_angle_entity, "azimuth_entity": self.azimuth_entity,
                "elevation_entity": self.elevation_entity}

    @classmethod
    def from_dict(cls, data: Any) -> CurtainSettings:
        data = data if isinstance(data, Mapping) else {}

        def entity(key: str, default: str | None) -> str | None:
            value = data.get(key, default)
            if value is None or value == "":
                return None if default is None or key == "sun_angle_entity" else default
            return value if isinstance(value, str) and "." in value else default

        return cls(
            tolerance=int(_number(data.get("tolerance"), 3, *TOLERANCE_LIMITS)),
            verify_timeout=int(_number(data.get("verify_timeout"), 120, *VERIFY_TIMEOUT_LIMITS)),
            sun_angle_entity=entity("sun_angle_entity", "sensor.sun_angle"),
            azimuth_entity=entity("azimuth_entity", "sun.sun") or "sun.sun",
            elevation_entity=entity("elevation_entity", "sun.sun") or "sun.sun",
        )


@dataclass(frozen=True, slots=True)
class CurtainConfig:
    windows: Mapping[str, Window] = field(default_factory=dict)
    settings: CurtainSettings = field(default_factory=CurtainSettings)

    def as_dict(self) -> dict[str, Any]:
        return {"windows": [w.as_dict() for w in self.windows.values()], "settings": self.settings.as_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> CurtainConfig:
        data = data if isinstance(data, Mapping) else {}
        windows: dict[str, Window] = {}
        owners: dict[str, str] = {}
        for item in data.get("windows") or ():
            if not isinstance(item, Mapping) or (window := Window.from_dict(item)) is None \
                    or window.window_id in windows:
                continue
            # A cover stored in two windows (only possible by editing the store by hand) stays with the first.
            kept = tuple(x for x in window.layers if owners.setdefault(x.entity_id, window.window_id) == window.window_id)
            if len(kept) != len(window.layers):
                window = with_layers(window, kept)
            windows[window.window_id] = window
            if len(windows) >= MAX_WINDOWS:
                break
        return cls(windows=windows, settings=CurtainSettings.from_dict(data.get("settings")))

    def owner_of(self, entity_id: str) -> str | None:
        return next((w.window_id for w in self.windows.values() if entity_id in w.entity_ids), None)

    def with_window(self, window: Window) -> CurtainConfig:
        return replace(self, windows={**self.windows, window.window_id: window})

    def without_window(self, window_id: str) -> CurtainConfig:
        return replace(self, windows={k: v for k, v in self.windows.items() if k != window_id})


# ------------------------------------------------------------------ defaults
def _preset(preset_id: str, name: str, layers: Iterable[CoverLayer], sheer: int, other: int) -> Preset:
    return Preset(preset_id, name, {x.layer_id: (sheer if x.kind == "sheer" else other) for x in layers})


def is_layered(layers: Iterable[CoverLayer]) -> bool:
    """A window with a sheer layer and at least one other layer (the Stova / Køkur south windows)."""
    kinds = [x.kind for x in layers]
    return "sheer" in kinds and any(k != "sheer" for k in kinds)


def default_presets(layers: tuple[CoverLayer, ...]) -> tuple[Preset, ...]:
    """Starting templates. Every position is editable; nothing is hard-coded after creation.

    Layered windows (sheer + blackout) get the five coordinated presets of the legacy system
    (Fult opið, Hálvt skugga, Fult skugga, Hálvt myrkt / fult skugga at night, Fult myrkt);
    every other window gets Fully open / Half open / Fully closed.
    """
    if not layers:
        return ()
    if is_layered(layers):
        return (
            _preset("p_open", "Fully open", layers, 100, 100),
            _preset("p_half_shade", "Half shade", layers, 50, 100),
            _preset("p_full_shade", "Full shade", layers, 0, 100),
            _preset("p_evening_privacy", "Evening privacy", layers, 0, 60),
            _preset("p_blackout", "Full blackout", layers, 100, 0),
        )
    return (
        _preset("p_open", "Fully open", layers, 100, 100),
        _preset("p_half", "Half open", layers, 50, 50),
        _preset("p_closed", "Fully closed", layers, 0, 0),
    )


def default_conditions(presets: tuple[Preset, ...]) -> dict[str, str | None]:
    """Suggested condition -> preset mapping for the default presets (editable; not used in this phase)."""
    ids = {p.preset_id for p in presets}

    def pick(*candidates: str) -> str | None:
        return next((c for c in candidates if c in ids), None)

    return {
        "morning": pick("p_half_shade", "p_half"),
        "day": pick("p_open"),
        "sun": pick("p_full_shade", "p_half"),
        "evening_privacy": pick("p_evening_privacy", "p_closed"),
        "night": pick("p_blackout", "p_closed"),
        "sleep": pick("p_blackout", "p_closed"),
        "manual_override": None,  # a manual move is kept as it is
    }


def with_layers(window: Window, layers: tuple[CoverLayer, ...]) -> Window:
    """New layers for a window. Existing presets keep their values; a new layer is "Leave unchanged"
    in every existing preset and a removed layer disappears from them. Presets are only created
    from the templates when the window has none yet (nothing is ever overwritten)."""
    ids = [x.layer_id for x in layers]
    if not window.presets and layers:
        presets = default_presets(layers)
        conditions = default_conditions(presets)
        return replace(window, layers=layers, presets=presets, conditions=conditions,
                       selected_preset=window.selected_preset)
    presets = tuple(Preset(p.preset_id, p.name, {i: p.positions.get(i) for i in ids}) for p in window.presets)
    return replace(window, layers=layers, presets=presets)
