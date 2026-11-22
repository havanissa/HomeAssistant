"""Specialised automation designers: graphical editors that translate into the ordinary rule model.

    room -> device -> designer (Lighting, Cover, ...) -> rules of the device -> one native automation

A designer is a document in ``device["designers"]`` (versioned, with a stable id). It is the single source of
truth for what it configures: its rules are *derived* from it every time the device is analysed or compiled
(``expand``), never stored or edited as rules. The derived rules are ordinary rules of the model; the compiler,
the conflict check, the summaries, the diagnostics and the blueprint export treat them like any other rule.
So a designer is a different interface to the same automation model, and nothing it generates needs this App
at run time.

Adding a designer = a module with ``KIND``, ``VERSION``, ``validate``, ``expand`` (and optionally
``capability_problems``, ``sources_of``) registered in :data:`REGISTRY`, an entry in :data:`APPLICABLE` and an
editor in the interface. A designer document of an unknown kind or of a newer version (written by a later
Automation Builder) is kept exactly as it is, reported, and blocks deployment of its device: it is never
dropped and never half-understood.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .. import model
from ..compiler import Input, plain
from ..model import Problems
from . import common as C, cover, lighting2, presence

#: The Lighting Designer module handles both versions: 1 (Phase 0.3, kept and compiled exactly as before) and 2.
REGISTRY = {lighting2.KIND: lighting2, cover.KIND: cover}
#: Which designers suit an entity of a domain. Every other domain keeps the ordinary rule editor.
APPLICABLE = {"light": ["lighting"], "cover": ["cover"]}
#: Kinds that are planned but have no designer yet (the gallery says so; there is no editor for them).
PLANNED = ("climate", "ventilation", "fan")
MAX_DESIGNERS = 4
MAX_DOCUMENT = 120_000


def new_designer_id() -> str:
    import secrets
    return f"g_{secrets.token_hex(4)}"


# ------------------------------------------------------------------ validation
def validate_all(raw: Any, targets: list[str], path: str, p: Problems) -> list[dict[str, Any]] | None:
    """``device["designers"]``: the canonical list, or None when there is none."""
    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > MAX_DESIGNERS:
        p.add(path, f"At most {MAX_DESIGNERS} designers per device")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw[:MAX_DESIGNERS]):
        here = f"{path}[{i}]"
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not C.DESIGNER_ID.match(item["id"]):
            p.add(f"{here}.id", "Invalid designer id")
            continue
        if any(d["id"] == item["id"] for d in out):
            p.add(f"{here}.id", "Two designers share an id")
            continue
        if not _plain_json(item) or len(json.dumps(item)) > MAX_DOCUMENT:
            p.add(here, "The designer document is too large")
            continue
        module = REGISTRY.get(item.get("kind"))
        version = item.get("version")
        if module is None or not isinstance(version, int) or isinstance(version, bool) or version > module.VERSION:
            out.append(copy.deepcopy(item))  # a later App's document: kept unchanged, never compiled here
            continue
        if any(d.get("kind") == module.KIND for d in out):
            p.add(here, f"A device has at most one {module.KIND} designer")
            continue
        out.append(module.validate(item, item["id"], targets, here, p))
    return out or None


def _plain_json(value: Any, depth: int = 0) -> bool:
    """Plain JSON data, at most 12 levels deep (a designer nests deeper than a rule's action data)."""
    if depth > 12:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_plain_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain_json(v, depth + 1) for k, v in value.items())
    return False


def supported(designer: dict[str, Any]) -> bool:
    module = REGISTRY.get(designer.get("kind"))
    version = designer.get("version")
    return (module is not None and isinstance(version, int) and not isinstance(version, bool)
            and 1 <= version <= module.VERSION)


def referenced_sources(designers: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for d in designers or []:
        if supported(d):
            out |= REGISTRY[d["kind"]].sources_of(d)
    return out


def check_references(config: dict[str, Any], p: Problems) -> None:
    """Every presence source a designer names must exist (a source in use cannot be deleted)."""
    known = set(((config.get("presence") or {}).get("sources") or {}))
    for device_id, device in config["devices"].items():
        missing = referenced_sources(device.get("designers")) - known
        if missing:
            p.add(f"devices.{device_id}.designers", "A designer refers to a presence source that does not exist: "
                  + ", ".join(sorted(missing)))


# ------------------------------------------------------------------ expansion
def input_key(*parts: Any) -> str:
    """A blueprint input identifier (at most 64 characters; a long one ends in a hash of all of it, so two
    different values never share an identifier)."""
    import re
    key = "_".join(re.sub(r"[^a-z0-9]+", "_", str(plain(p)).lower()).strip("_") for p in parts if p not in (None, ""))
    key = key if key[:1].isalpha() else f"i_{key}"
    return key if len(key) <= 64 else key[:52].rstrip("_") + "_" + hashlib.sha256(key.encode()).hexdigest()[:8]


class Expansion:
    """Everything one expansion of a device's designers produces.

    ``exposed`` (blueprint export only): input key -> :class:`compiler.Input`. Every value a designer puts into a
    rule passes :meth:`v` (or :meth:`e` for entities): normally that registers it as a *candidate* blueprint input
    and returns it unchanged; for an exposed key it returns the Input, which the compiler passes through.
    """

    def __init__(self, device: dict[str, Any], presence_config: dict[str, Any] | None, *,
                 exposed: dict[str, Input] | None = None) -> None:
        self.device = device
        self.targets = device["targets"]
        self.sources = copy.deepcopy((presence_config or {}).get("sources") or {})
        self.refs = C.Refs()
        self.rules: list[dict[str, Any]] = []
        self.problems: list[dict[str, Any]] = []
        self.conflicts: list[dict[str, Any]] = []
        self.views: dict[str, Any] = {}
        self.params: dict[str, dict[str, Any]] = {}
        self.exposed = exposed or {}
        self.blueprint = exposed is not None
        self.fixed: list[str] = []
        self._own_ids = {r["id"] for r in device["rules"]}
        self.extra_targets: set[str] = set()   # helpers a designer's rules may act on (Lighting Designer 2)
        self.generators: dict[str, Any] = {}    # designer id -> its expansion (for the simulation and live views)

    # ------------------------------------------------------------ results
    def problem(self, level: str, code: str, message: str, designer_id: str | None = None, layer: str | None = None) -> None:
        item = C.problem(level, code, message, designer_id, layer)
        if item not in self.problems:
            self.problems.append(item)

    def conflict(self, key: str, code: str, message: str, rule_ids: list[str], entity_id: str | None) -> None:
        if any(c["key"] == key for c in self.conflicts):
            return
        self.conflicts.append({"key": key, "code": code, "level": "blocking", "message": message, "rule_ids": rule_ids,
                               "entity_id": entity_id, "acknowledged": key in set(self.device.get("acknowledged") or [])})

    def add_rule(self, raw: dict[str, Any], designer_id: str) -> None:
        if len(self.rules) >= C.MAX_GENERATED:
            self.problem("blocking", "too_many", f"The designers of this device would generate more than {C.MAX_GENERATED} "
                         "rules. Use fewer columns or lights per designer.", designer_id)
            return
        if raw["id"] in self._own_ids or any(r["id"] == raw["id"] for r in self.rules):
            self.problem("blocking", "internal", f"Internal error: the generated rule id {raw['id']} is not unique. "
                         "Please report this.", designer_id)
            return
        if self.blueprint:
            self.rules.append(raw)
            return
        clean = C.checked(raw, [*self.targets, *sorted(self.extra_targets)], self.problems, designer_id)
        if clean is not None:
            self.rules.append(clean)

    # ------------------------------------------------------------ values that can become blueprint inputs
    def v(self, key: str, value: Any, *, kind: str, group: str, name: str, description: str,
          selector: dict[str, Any], default: Any = None) -> Any:
        if value in (None, "", [], {}):
            return value
        if key in self.exposed:
            return self.exposed[key]
        if not self.blueprint and key not in self.params:
            self.params[key] = {"key": key, "group": group, "kind": kind, "value": value, "name": name[:80],
                                "description": description, "selector": selector,
                                "default": value if default is None else default, "paths": []}
        return value

    def e(self, entity_id: Any, use: str, *, group: str = "Sensor entities", name: str | None = None) -> Any:
        if not entity_id:
            return entity_id
        if isinstance(entity_id, Input):
            return entity_id
        domain, object_id = entity_id.split(".", 1)
        controlled = entity_id in self.targets
        key = f"controlled_{C.slug(object_id)}" if controlled else f"{domain}_{C.slug(object_id)}"
        return self.v(key, entity_id, kind="entity", group="Controlled entities" if controlled else group,
                      name=name or entity_id, description=f"{'Controlled entity' if controlled else use}. In the "
                      f"original: {entity_id}.", selector={"entity": {"domain": domain}})

    def seconds(self, key: str, value: Any, name: str, description: str) -> Any:
        if not plain(value):
            return value
        hours, rest = divmod(int(plain(value)), 3600)
        minutes, secs = divmod(rest, 60)
        return self.v(key, value, kind="duration", group="Delays and durations", name=name, description=description,
                      selector={"duration": {}}, default={"hours": hours, "minutes": minutes, "seconds": secs})

    def number(self, key: str, value: Any, name: str, description: str, *, group: str = "Thresholds",
               selector: dict[str, Any] | None = None) -> Any:
        return self.v(key, value, kind="number", group=group, name=name, description=description,
                      selector=selector or {"number": {"mode": "box", "step": "any"}})

    def text(self, key: str, value: Any, name: str, description: str, *, group: str = "States") -> Any:
        return self.v(key, value, kind="text", group=group, name=name, description=description, selector={"text": {}})

    def time(self, key: str, value: Any, name: str, description: str) -> Any:
        return self.v(key, value, kind="time", group="Time values", name=name, description=description, selector={"time": {}})


def expand(device: dict[str, Any], presence_config: dict[str, Any] | None, *, index: Any = None,
           exposed: dict[str, Input] | None = None) -> Expansion:
    """The rules, problems and conflicts of a device's designers (nothing is saved or sent)."""
    x = Expansion(device, presence_config, exposed=exposed)
    for d in device.get("designers") or []:
        if not supported(d):
            x.problem("blocking", "unsupported", f"A designer of the kind “{d.get('kind')}” (version {d.get('version')}) "
                      "was made by another version of Automation Builder. It is kept unchanged, but this version "
                      "cannot compile it: update Automation Builder, or remove it.", d.get("id"))
            continue
        module = REGISTRY[d["kind"]]
        if not device["enabled"]:
            x.views[d["id"]] = {"rules": [], "resolved": module.resolved_views(d, x), "primary": None}
            continue
        x.views[d["id"]] = module.expand(d, x)
        if index is not None and d["enabled"]:
            module.capability_problems(d, capabilities(index, d.get("lights") or d.get("covers") or []), x)
            if hasattr(module, "index_problems"):
                module.index_problems(d, index, x)
    for d in device.get("designers") or []:
        if supported(d) and d["enabled"]:
            others = sorted({e for r in device["rules"] if r["enabled"] for a in model.walk_actions(r["actions"])
                             for e in model.action_entities(a, device["targets"])}
                            & set(d.get("lights") or d.get("covers") or []))
            if others:
                x.problem("warning", "designer_overlap", f"“{d['name']}” and ordinary rules of this device both control "
                          f"{', '.join(others)}. Both act; when they can act at the same moment the conflict check "
                          "below asks you to decide. Neither has priority by itself.", d["id"])
    return x


def effective(device: dict[str, Any], expansion: Expansion) -> dict[str, Any]:
    """The device as the compiler sees it: its own rules, then the rules its designers generate."""
    if not expansion.rules:
        return device
    return {**device, "rules": [*device["rules"], *expansion.rules]}


# ------------------------------------------------------------------ capabilities
COVER_FEATURES = {"open": 1, "close": 2, "position": 4, "stop": 8, "tilt_open": 16, "tilt_close": 32, "tilt": 128}
COLOR_MODES = ("hs", "xy", "rgb", "rgbw", "rgbww")


def capability(index: Any, entity_id: str) -> dict[str, Any]:
    """What Home Assistant reports an entity can do (lights: colour modes and effects; covers: features)."""
    state = index.states.get(entity_id)
    domain = entity_id.split(".", 1)[0]
    base = {"entity_id": entity_id, "name": index.name(entity_id), "domain": domain,
            "designers": APPLICABLE.get(domain, [])}
    if state is None or state.get("state") == "unavailable":
        return {**base, "unknown": True}
    attributes = state.get("attributes") or {}
    if domain == "light":
        modes = attributes.get("supported_color_modes")
        if not isinstance(modes, list):
            return {**base, "unknown": True}
        effects = attributes.get("effect_list")
        return {**base, "unknown": False, "modes": modes,
                "brightness": any(m != "onoff" for m in modes),
                "color_temp": "color_temp" in modes,
                "color": any(m in COLOR_MODES for m in modes),
                "effect": isinstance(effects, list) and bool(effects),
                "effects": effects if isinstance(effects, list) else None,
                "min_kelvin": attributes.get("min_color_temp_kelvin"), "max_kelvin": attributes.get("max_color_temp_kelvin")}
    if domain == "cover":
        features = attributes.get("supported_features")
        if not isinstance(features, int):
            return {**base, "unknown": True}
        return {**base, "unknown": False, "features": features,
                **{name: bool(features & bit) for name, bit in COVER_FEATURES.items()},
                "device_class": attributes.get("device_class"),
                "reports_position": "current_position" in attributes,
                "reports_tilt": "current_tilt_position" in attributes,
                "weather_sensitive": attributes.get("device_class") == "awning"}
    return {**base, "unknown": False}


def capabilities(index: Any, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
    return {e: capability(index, e) for e in entity_ids}


def applicable(targets: list[str]) -> list[str]:
    out: list[str] = []
    for target in targets:
        for kind in APPLICABLE.get(target.split(".", 1)[0], []):
            if kind not in out:
                out.append(kind)
    return out


def blank(kind: str, designer_id: str, targets: list[str], name: str | None = None) -> dict[str, Any]:
    """A new, empty designer of a kind: no rows (the user ticks the lights or covers that take part), every
    control off, nothing set."""
    module = REGISTRY[kind]
    p = Problems()
    raw = {"id": designer_id, "kind": kind, "version": module.VERSION, "name": name or module.DEFAULT_NAME, module.ROWS: []}
    clean = module.validate(raw, designer_id, targets, "designer", p)
    assert not p.items, p.items
    return clean
