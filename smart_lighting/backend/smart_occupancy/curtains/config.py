"""Curtain configuration: windows, cover layers, presets and settings (0.6.1, automatic rules 0.6.2,
solar and thermal conditions 0.6.3, Global Curtain Modes 0.6.4).

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

# Operating modes of a window (0.6.2): Off, Observing (decides and explains, sends nothing) and
# Automatic (sends the decided preset). Automatic is only set by the explicit enable action.
WINDOW_MODES: tuple[str, ...] = ("off", "observing", "automatic")
PERMITTED_MODES: frozenset[str] = frozenset(WINDOW_MODES)
DEFAULT_MODE = "observing"

COMPASS: dict[str, float] = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0, "SW": 225.0, "W": 270.0,
                             "NW": 315.0}

# Automatic conditions -> the preset each one uses (per window). "Manual override" never sends a
# command (the manual position is kept), and "Waiting for daylight" keeps the current position, so
# neither of them has a preset of its own. "Glare protection" and "Excess solar heat" (0.6.3) use the
# Direct sunlight preset until a preset of their own is chosen; "Useful solar heating" keeps the
# Morning / Normal daytime preset, so it has no mapping either.
CONDITIONS: tuple[str, ...] = ("morning", "day", "sun", "glare", "heat", "evening_privacy", "night", "sleep",
                               "lamp", "manual_override")
SUN_FALLBACK: frozenset[str] = frozenset({"glare", "heat"})   # unset -> the Direct sunlight preset
CONDITION_WORDS = {
    "morning": "Morning", "day": "Normal daytime", "sun": "Direct sunlight", "evening_privacy": "Evening privacy",
    "night": "Night", "sleep": "Sleep", "lamp": "Window lamp on", "manual_override": "Manual override",
    "waiting": "Waiting for daylight", "glare": "Glare protection", "heat": "Excess solar heat",
    "solar_heating": "Useful solar heating",
}

# Global Curtain Modes (0.6.4): the conditions a mapping can be set for. The first six are the common
# modes on the Global Modes page; the others are under Advanced. Manual override never has a preset.
MODES: tuple[str, ...] = ("morning", "day", "evening_privacy", "night", "sleep", "sun", "lamp", "glare", "heat")
COMMON_MODES: tuple[str, ...] = MODES[:6]
ADVANCED_MODES: tuple[str, ...] = MODES[6:]
MODE_WORDS = {
    "morning": "Morning", "day": "Daytime", "evening_privacy": "Evening", "night": "Night", "sleep": "Sleep",
    "sun": "Sun Protection", "lamp": "Window Lamp", "glare": "Glare", "heat": "Excessive Heat",
    "solar_heating": "Useful solar heating", "waiting": "Waiting for daylight", "manual_override": "Manual override",
}
NO_ACTION = "no_action"   # an explicit "this mode does not control this target" (not the same as missing)

MAX_WINDOWS = 64
MAX_LAYERS = 6
MAX_PRESETS = 24
MAX_COVER_PRESETS = 12
MAX_OBSTRUCTIONS = 4
NAME_MAX = 60

TOLERANCE_LIMITS = (0, 10)          # percentage points: "at the position" / "already there"
VERIFY_TIMEOUT_LIMITS = (20, 600)   # seconds to wait for the reported position
EXPOSURE_WIDTH_LIMITS = (5, 90)     # ± degrees either side of the window's bearing
ELEVATION_LIMITS = (-5.0, 60.0)
MAX_ELEVATION_LIMITS = (5.0, 90.0)  # an overhang / eaves: above this the sun no longer reaches the glass


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
class CoverPreset:
    """A named position of one cover (0.6.4), used when that cover is assigned on its own."""

    preset_id: str
    name: str
    position: int

    def as_dict(self) -> dict[str, Any]:
        return {"preset_id": self.preset_id, "name": self.name, "position": self.position}

    @classmethod
    def from_dict(cls, data: Any) -> CoverPreset | None:
        if not isinstance(data, Mapping):
            return None
        preset_id, name = data.get("preset_id"), data.get("name")
        if not isinstance(preset_id, str) or preset_id == NO_ACTION or not isinstance(name, str) or not name.strip():
            return None
        try:
            position = valid_position(data.get("position"))
        except CurtainConfigError:
            return None
        return None if position is None else cls(preset_id, name.strip()[:NAME_MAX], position)


def default_cover_presets() -> tuple[CoverPreset, ...]:
    """Starting presets of one cover; every name and position is editable (and more can be added)."""
    return (CoverPreset("c_open", "Fully open", 100), CoverPreset("c_half", "Half open", 50),
            CoverPreset("c_closed", "Fully closed", 0))


@dataclass(frozen=True, slots=True)
class CoverLayer:
    layer_id: str
    entity_id: str
    kind: str = "custom"
    label: str | None = None  # optional own name, e.g. "Ljós"
    # 0.6.4: this cover's own presets (for assigning it on its own); see default_cover_presets().
    presets: tuple[CoverPreset, ...] = field(default_factory=default_cover_presets)

    def as_dict(self) -> dict[str, Any]:
        return {"layer_id": self.layer_id, "entity_id": self.entity_id, "kind": self.kind, "label": self.label,
                "presets": [p.as_dict() for p in self.presets]}

    def preset(self, preset_id: str | None) -> CoverPreset | None:
        return next((p for p in self.presets if p.preset_id == preset_id), None) if preset_id else None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CoverLayer | None:
        layer_id, entity_id = data.get("layer_id"), data.get("entity_id")
        if not isinstance(layer_id, str) or not isinstance(entity_id, str) or not entity_id.startswith("cover."):
            return None
        kind = data.get("kind")
        label = data.get("label")
        raw = data.get("presets")
        if isinstance(raw, (list, tuple)):
            presets: list[CoverPreset] = []
            for item in raw:
                if (p := CoverPreset.from_dict(item)) is not None and all(x.preset_id != p.preset_id for x in presets):
                    presets.append(p)
            cover_presets = tuple(presets[:MAX_COVER_PRESETS])
        else:
            cover_presets = default_cover_presets()   # stored before 0.6.4: the starting presets
        return cls(layer_id, entity_id, kind if kind in COVER_KINDS else "custom",
                   label if isinstance(label, str) and label.strip() else None, cover_presets)


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
        if preset_id == NO_ACTION:
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
    # 0.6.3: an overhang, balcony or eaves above the window: a higher (summer) sun does not reach the
    # glass. None = no overhang (the 0.6.2 behaviour).
    max_elevation: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"exposure_width": self.exposure_width, "min_elevation": self.min_elevation,
                "obstructions": [o.as_dict() for o in self.obstructions], "max_elevation": self.max_elevation}

    @classmethod
    def from_dict(cls, data: Any) -> SolarSettings:
        data = data if isinstance(data, Mapping) else {}
        obstructions: list[Obstruction] = []
        for item in data.get("obstructions") or ():
            try:
                obstructions.append(Obstruction.from_dict(item))
            except CurtainConfigError:
                continue
        top = data.get("max_elevation")
        return cls(
            exposure_width=int(_number(data.get("exposure_width"), 70, *EXPOSURE_WIDTH_LIMITS)),
            min_elevation=round(_number(data.get("min_elevation"), 3.0, *ELEVATION_LIMITS), 1),
            obstructions=tuple(obstructions[:MAX_OBSTRUCTIONS]),
            max_elevation=None if top is None or isinstance(top, bool) or not isinstance(top, (int, float))
            else round(_number(top, 90.0, *MAX_ELEVATION_LIMITS), 1),
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
    # Window overrides: condition -> preset id, NO_ACTION, or missing / None = inherit (0.6.4; in 0.6.3 this
    # was the only mapping and None meant "keep the position", which inheriting nothing still means).
    conditions: Mapping[str, str | None] = field(default_factory=dict)
    # Cover overrides (0.6.4): layer id -> {mode: cover preset id or NO_ACTION}; only what is set.
    cover_modes: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # Automatic rule settings set for this window only (see rules.py); everything else is inherited.
    auto: Mapping[str, Any] = field(default_factory=dict)
    # Automations an administrator reviewed and allowed to coexist with Automatic for these covers.
    allowed_automations: tuple[str, ...] = ()
    # App 0.8.0: this window's own sunlight sensors (multisun.py); off unless switched on with at least one sensor.
    multi_sun: bool = False
    sun_sensors: tuple[Any, ...] = ()

    @property
    def multi_sun_active(self) -> bool:
        """Multi-sensor sunlight is the window's sun evidence (switched on, at least one enabled sensor)."""
        return self.multi_sun and any(s.enabled for s in self.sun_sensors)

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
            "cover_modes": {k: dict(v) for k, v in self.cover_modes.items() if v},
            "auto": _rules().dump(self.auto), "allowed_automations": list(self.allowed_automations),
            **({"multi_sun": self.multi_sun, "sun_sensors": [x.as_dict() for x in self.sun_sensors]}
               if self.multi_sun or self.sun_sensors else {}),
        }

    @property
    def layered(self) -> bool:
        """More than one cover: single covers of it can be assigned on their own (0.6.4)."""
        return len(self.layers) > 1

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
        raw_cover = data.get("cover_modes") if isinstance(data.get("cover_modes"), Mapping) else {}
        cover_modes: dict[str, dict[str, str]] = {}
        for layer in layers:
            item = raw_cover.get(layer.layer_id)
            if isinstance(item, Mapping):
                kept = {m: v for m, v in item.items() if m in MODES and isinstance(v, str)
                        and (v == NO_ACTION or layer.preset(v) is not None)}
                if kept:
                    cover_modes[layer.layer_id] = kept
        selected = data.get("selected_preset")
        area_id = data.get("area_id")
        return cls(
            window_id=window_id,
            name=name.strip()[:NAME_MAX],
            area_id=area_id if isinstance(area_id, str) and area_id else None,
            layers=tuple(layers[:MAX_LAYERS]),
            direction=direction if direction in COMPASS and bearing is None else None,
            bearing=bearing,
            # An unknown stored mode is kept as Observing: nothing moves on its own.
            mode=mode if mode in PERMITTED_MODES else DEFAULT_MODE,
            solar_protection=data.get("solar_protection") is True,
            privacy=data.get("privacy") is True,
            sleep_protection=data.get("sleep_protection") is True,
            solar=SolarSettings.from_dict(data.get("solar")),
            presets=tuple(presets[:MAX_PRESETS]),
            selected_preset=selected if selected in preset_ids else None,
            conditions={c: (v if (v := raw_conditions.get(c)) in preset_ids or (v == NO_ACTION and c in MODES)
                            else None) for c in CONDITIONS},
            cover_modes=cover_modes,
            auto=_rules().load(data.get("auto"), "window", legacy=True),
            allowed_automations=tuple(a for a in data.get("allowed_automations") or ()
                                      if isinstance(a, str) and a.startswith("automation."))[:64],
            multi_sun=data.get("multi_sun") is True,
            sun_sensors=_multisun().sensors_from(data.get("sun_sensors")),
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
class Assignment:
    """One Global Modes assignment (0.6.4): a whole window (``layer_id`` None) or one cover of a layered
    window, and the stable id of the preset it uses (a window preset, or that cover's own preset)."""

    window_id: str
    layer_id: str | None
    preset_id: str

    @property
    def target(self) -> tuple[str, str | None]:
        return self.window_id, self.layer_id

    def as_dict(self) -> dict[str, Any]:
        return {"window_id": self.window_id, "layer_id": self.layer_id, "preset_id": self.preset_id}

    @classmethod
    def from_dict(cls, data: Any) -> Assignment | None:
        if not isinstance(data, Mapping):
            return None
        window_id, layer_id, preset_id = data.get("window_id"), data.get("layer_id"), data.get("preset_id")
        if not isinstance(window_id, str) or not isinstance(preset_id, str) or preset_id == NO_ACTION:
            return None
        if layer_id is not None and not isinstance(layer_id, str):
            return None
        return cls(window_id, layer_id or None, preset_id)


def overlapping(items: Iterable[Assignment], window_id: str, layer_id: str | None) -> list[Assignment]:
    """Assignments of one mode that would control the same covers as (window_id, layer_id): the same
    target, the whole window and one of its covers, or one of its covers and the whole window."""
    return [a for a in items if a.window_id == window_id
            and (a.layer_id == layer_id or a.layer_id is None or layer_id is None)]


@dataclass(frozen=True, slots=True)
class CurtainConfig:
    windows: Mapping[str, Window] = field(default_factory=dict)
    settings: CurtainSettings = field(default_factory=CurtainSettings)
    # Automatic rule settings for every window, and per room (area id); see rules.py.
    defaults: Mapping[str, Any] = field(default_factory=dict)
    rooms: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Global Curtain Modes (0.6.4): mode -> assignments, and room overrides: area id -> {mode: preset id
    # (resolved in each window of the room) or NO_ACTION}.
    modes: Mapping[str, tuple[Assignment, ...]] = field(default_factory=dict)
    room_modes: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # 0.6.6 (Phase 4.6.4 addendum): Global Schedule and Global Modes are the only configuration of schedules and
    # mode presets. False only for a store from 0.6.5 or earlier until its reviewed migration is applied:
    # until then old room / window / cover settings keep working exactly as before (never silently changed).
    centralized: bool = True

    def as_dict(self) -> dict[str, Any]:
        rules = _rules()
        return {"windows": [w.as_dict() for w in self.windows.values()], "settings": self.settings.as_dict(),
                "defaults": rules.dump(self.defaults),
                "rooms": {k: rules.dump(v) for k, v in self.rooms.items() if v},
                "modes": {m: [a.as_dict() for a in self.modes.get(m, ())] for m in MODES if self.modes.get(m)},
                "room_modes": {k: dict(v) for k, v in self.room_modes.items() if v},
                "centralized": self.centralized}

    def assignment(self, mode: str, window_id: str, layer_id: str | None) -> Assignment | None:
        """The assignment of this exact target in a mode."""
        return next((a for a in self.modes.get(mode, ()) if a.target == (window_id, layer_id)), None)

    def rules_for(self, window: Window) -> tuple[dict[str, Any], dict[str, str]]:
        """The effective automatic settings of a window and where each comes from."""
        return _rules().resolve(self.defaults, self.rooms.get(window.area_id or "") if window.area_id else None,
                                window.auto, legacy=not self.centralized)

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
        rules = _rules()
        rooms_raw = data.get("rooms") if isinstance(data.get("rooms"), Mapping) else {}
        rooms = {k: v for k, raw in rooms_raw.items()
                 if isinstance(k, str) and (v := rules.load(raw, "room", legacy=True))}
        # Global Modes (0.6.4; absent before). An assignment of a window or cover that no longer exists, or a
        # second one for covers already assigned in that mode, is dropped. One whose preset is missing is
        # kept (it acts as No action and is reported) so nothing is silently re-pointed.
        modes: dict[str, tuple[Assignment, ...]] = {}
        raw_modes = data.get("modes") if isinstance(data.get("modes"), Mapping) else {}
        for mode in MODES:
            kept: list[Assignment] = []
            for item in raw_modes.get(mode) or ():
                a = Assignment.from_dict(item)
                window = windows.get(a.window_id) if a else None
                if window is None or (a.layer_id is not None and (not window.layered or window.layer(a.layer_id)
                                                                   is None)):
                    continue
                if not overlapping(kept, a.window_id, a.layer_id):
                    kept.append(a)
            if kept:
                modes[mode] = tuple(kept)
        raw_rooms = data.get("room_modes") if isinstance(data.get("room_modes"), Mapping) else {}
        room_modes = {k: kept for k, v in raw_rooms.items() if isinstance(k, str) and isinstance(v, Mapping)
                      and (kept := {m: x for m, x in v.items() if m in MODES and isinstance(x, str) and x})}
        return cls(windows=windows, settings=CurtainSettings.from_dict(data.get("settings")),
                   defaults=rules.load(data.get("defaults"), "global"), rooms=rooms, modes=modes,
                   room_modes=room_modes,
                   # A store written before 0.6.6 has no flag: its old settings stay active until migrated.
                   centralized=data.get("centralized") is True or not data)

    def owner_of(self, entity_id: str) -> str | None:
        return next((w.window_id for w in self.windows.values() if entity_id in w.entity_ids), None)

    def with_window(self, window: Window) -> CurtainConfig:
        return replace(self, windows={**self.windows, window.window_id: window})._pruned(window.window_id)

    def without_window(self, window_id: str) -> CurtainConfig:
        return replace(self, windows={k: v for k, v in self.windows.items() if k != window_id})._pruned(window_id)

    def with_modes(self, modes: Mapping[str, Iterable[Assignment]]) -> CurtainConfig:
        return replace(self, modes={m: tuple(v) for m, v in modes.items() if m in MODES and v})

    def _pruned(self, window_id: str) -> CurtainConfig:
        """Global assignments of a window follow its changes: a removed window, a removed cover or a
        deleted preset takes its assignments with it (the preset's positions were never copied)."""
        window = self.windows.get(window_id)
        changed = False
        modes: dict[str, tuple[Assignment, ...]] = {}
        for mode, items in self.modes.items():
            kept = []
            for a in items:
                if a.window_id == window_id:
                    ok = window is not None and (
                        window.preset(a.preset_id) is not None if a.layer_id is None else
                        window.layered and (layer := window.layer(a.layer_id)) is not None
                        and layer.preset(a.preset_id) is not None)
                    if not ok:
                        changed = True
                        continue
                kept.append(a)
            if kept:
                modes[mode] = tuple(kept)
        return replace(self, modes=modes) if changed else self


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
    """The condition -> preset mapping 0.6.1-0.6.3 gave a new window (kept for reference and tests; 0.6.4
    creates new windows without a mapping)."""
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
        "lamp": pick("p_half_shade", "p_half"),
        "glare": None,            # 0.6.3: not set = the Direct sunlight preset
        "heat": None,
        "manual_override": None,  # a manual move is kept as it is
    }


def _multisun():
    from . import multisun  # noqa: PLC0415 - App 0.8.0 module

    return multisun


def _rules():
    from . import rules  # rules.py imports CurtainConfigError from here

    return rules


def with_layers(window: Window, layers: tuple[CoverLayer, ...]) -> Window:
    """New layers for a window. Existing presets keep their values; a new layer is "Leave unchanged"
    in every existing preset and a removed layer disappears from them. Presets are only created
    from the templates when the window has none yet (nothing is ever overwritten)."""
    ids = [x.layer_id for x in layers]
    cover_modes = {k: v for k, v in window.cover_modes.items() if k in ids}
    if not window.presets and layers:
        # 0.6.4: a new window starts without any mapping (every mode inherits from Global Modes, where it is
        # assigned explicitly); no preset is chosen for it by default.
        return replace(window, layers=layers, presets=default_presets(layers), cover_modes=cover_modes,
                       selected_preset=window.selected_preset)
    presets = tuple(Preset(p.preset_id, p.name, {i: p.positions.get(i) for i in ids}) for p in window.presets)
    return replace(window, layers=layers, presets=presets, cover_modes=cover_modes)
